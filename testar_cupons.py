#!/usr/bin/env python3
"""
testar_cupons.py — testador de cupons do checkout Ingresse, em alta performance.

Como funciona
-------------
O site valida um cupom com um POST em /v1/events/{evento}/tickets. O script
dispara essa mesma chamada para uma lista de códigos usando UM processo curl por
lote, em modo --parallel com HTTP/2 multiplexado e gzip. Assim some o custo de
abrir processo + handshake TLS (~1,2 s) por código, que era o maior gargalo.

Os dados do evento (event_id e producer_id) podem vir de três formas:
  --url <link do carrinho>   extrai o event_id e descobre o resto sozinho
  --event <id>               descobre o producer_id pela API do evento
  --event <id> --producer <id>   informa tudo manualmente

O script também respeita o rate limit do Cloudflare: se a API responder 429, ele
lê o Retry-After, grava o progresso no cache e para — em vez de insistir e
prolongar o bloqueio.

Uso
---
    python3 testar_cupons.py cupons.txt
    python3 testar_cupons.py cupons.txt --url https://cart.ingresse.com/<id>/tickets
    python3 testar_cupons.py cupons.txt --event <id> --workers 30 --out-csv r.csv
    python3 testar_cupons.py cupons.txt --info        # só mostra os dados do evento
    python3 testar_cupons.py cupons.txt --retest      # ignora o cache
    python3 testar_cupons.py cupons.txt --api-base http://127.0.0.1:8765   # teste local

Códigos de saída: 0 ok | 2 falhas | 3 bloqueado por rate limit
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

DEFAULT_EVENT = "a5009ec2-b981-4803-8bb1-41e66e78f936"
DEFAULT_PRODUCER = "c113d324-4009-4388-9a4b-accef90e246b"
DEFAULT_API = "https://apigw-purchase.prod.ingresse.com"
BASE_HEADERS = [
    "accept: application/json",
    "content-type: application/json",
    "authorization;",  # header vazio, igual ao que o site envia
    "origin: https://cart.ingresse.com",
    "referer: https://cart.ingresse.com/",
]


# ------------------------------------------------------------------ utilidades
def normalizar(codigo: str) -> str:
    """Maiúsculas, sem acento e sem símbolos — como o site trata o campo."""
    c = unicodedata.normalize("NFKD", codigo).encode("ascii", "ignore").decode()
    return "".join(ch for ch in c.upper() if ch.isalnum())


def carregar_codigos(arquivos):
    vistos, saida = set(), []
    for arq in arquivos:
        p = Path(arq)
        if not p.exists():
            sys.exit(f"arquivo não encontrado: {arq}")
        for linha in p.read_text(encoding="utf-8").splitlines():
            for bruto in linha.replace(",", "\n").replace(";", "\n").splitlines():
                c = normalizar(bruto)
                if c and c not in vistos:
                    vistos.add(c)
                    saida.append(c)
    return saida


def carregar_cache(caminho):
    cache = {}
    p = Path(caminho)
    if not p.exists():
        return cache
    for linha in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
        except json.JSONDecodeError:
            continue  # linha truncada por interrupção
        if r.get("valid") is not None:
            cache[r["code"]] = r
    return cache


def esc(s: str) -> str:
    """Escapa valor para arquivo de configuração do curl."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def extrair_event_id(valor: str) -> str:
    """Aceita o link do carrinho ou o próprio ID do evento."""
    valor = valor.strip()
    m = re.search(r"cart\.ingresse\.com/([0-9a-fA-F-]{36})", valor)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[0-9a-fA-F-]{36}", valor):
        return valor.lower()
    sys.exit(
        "não identifiquei o ID do evento em "
        f"'{valor}'.\n"
        "Use o link do carrinho (cart.ingresse.com/<ID>/tickets) ou passe o ID direto."
    )


def curl_get(url, timeout=20):
    """GET simples via curl (o requests do Python é bloqueado pelo Cloudflare)."""
    proc = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-H", "accept: application/json",
         "-H", "authorization;", "-H", "origin: https://cart.ingresse.com",
         "-H", "referer: https://cart.ingresse.com/", url],
        capture_output=True, text=True,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def descobrir_evento(event, api_base, timeout=20):
    """
    Consulta GET /v1/events/{id} e devolve os dados que importam:
    nome, produtora, se aceita cupom e se pede passkey.
    Retorna None se a API não responder como esperado.
    """
    j = curl_get(f"{api_base.rstrip('/')}/v1/events/{event}", timeout)
    if not j:
        return None
    data = j.get("data") or j
    if not isinstance(data, dict) or not data.get("name"):
        return None  # resposta não parece um evento (ID errado ou recurso diferente)
    org = data.get("organization") or {}
    return {
        "id": data.get("id") or event,
        "name": data.get("name"),
        "slug": data.get("slug"),
        "producer_id": org.get("id"),
        "producer_name": org.get("name"),
        "has_coupon": data.get("has_coupon"),
        "has_passkey": data.get("has_passkey"),
        "place": (data.get("place") or {}).get("name"),
    }


# ---------------------------------------------------------------------- runner
def montar_config(codigos, url, event, producer, timeout, pasta):
    blocos = []
    for i, c in enumerate(codigos):
        corpo = json.dumps({
            "event_id": event,
            "producer_id": producer,
            "providers": [],
            "user_document": "",
            "user_id": "",
            "code": c,
        }, separators=(",", ":"))
        linhas = [
            f'url = "{url}"',
            'request = "POST"',
            *[f'header = "{h}"' for h in BASE_HEADERS],
            f'data = "{esc(corpo)}"',
            f'output = "{pasta / f"{i:05d}.json"}"',
            "silent",
            "http2",
            "compressed",
            f"max-time = {timeout}",
            'write-out = "%{filename_effective}\\t%{http_code}\\n"',
        ]
        blocos.append("\n".join(linhas))
    return "\nnext\n".join(blocos) + "\n"


def rodar_lote(codigos, url, event, producer, workers, timeout):
    """
    Dispara um lote num único processo curl.
    Retorna ({codigo: (status_http, corpo)}, retry_after|None).
    """
    pasta = Path(tempfile.mkdtemp(prefix="cupons_"))
    try:
        cfg = pasta / "curl.cfg"
        cfg.write_text(montar_config(codigos, url, event, producer, timeout, pasta), encoding="utf-8")
        cmd = [
            "curl", "--config", str(cfg),
            "--parallel", "--parallel-max", str(workers),
            "--silent", "--show-error",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)

        # mapeia arquivo de saída -> status HTTP
        status = {}
        for linha in proc.stdout.splitlines():
            if "\t" not in linha:
                continue
            caminho, codigo = linha.rsplit("\t", 1)
            try:
                status[Path(caminho).name] = int(codigo)
            except ValueError:
                continue

        saida, retry_after = {}, None
        for i, c in enumerate(codigos):
            nome = f"{i:05d}.json"
            arq = pasta / nome
            corpo = arq.read_text(encoding="utf-8", errors="replace") if arq.exists() else None
            st = status.get(nome, 0)
            saida[c] = (st, corpo)
            if st == 429:
                retry_after = max(retry_after or 0, ler_retry_after(corpo))
        return saida, retry_after
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def ler_retry_after(corpo):
    """A Cloudflare manda 'error code: 1015' e o Retry-After vem no header; sem
    header acessível aqui, usamos um padrão conservador de 30 min."""
    return 1800


def interpretar(codigo, status, texto):
    """(registro, tipo) — tipo: 'ok' | 'retry' | 'rate'."""
    if status == 429:
        return None, "rate"
    if status == 0 or not texto:
        return None, "retry"
    if status >= 500:
        return None, "retry"
    try:
        j = json.loads(texto)
    except json.JSONDecodeError:
        return None, "retry"  # HTML de bloqueio/erro

    data = j.get("data") or {}
    cupom = data.get("coupon")
    if cupom:
        return {
            "code": codigo, "valid": True,
            "discount": cupom.get("discount_amount"),
            "type": cupom.get("discount_type"),
            "usages": cupom.get("current_usages"),
            "max_usages": cupom.get("max_usages"),
            "per_user": cupom.get("max_uses_per_user"),
            "start": cupom.get("start_date"),
            "end": cupom.get("end_date"),
            "status": cupom.get("status"),
            "msg": "válido",
        }, "ok"
    extras = data.get("extras") or {}
    return {
        "code": codigo, "valid": False,
        "msg": extras.get("message") or extras.get("code") or "inválido",
    }, "ok"


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Testa códigos de cupom no checkout da Ingresse.")
    ap.add_argument("listas", nargs="+", help="arquivo(s) com códigos (um por linha)")
    ap.add_argument("--url", help="link do carrinho (cart.ingresse.com/<id>/tickets) ou ID do evento")
    ap.add_argument("--event", default=None, help="ID do evento (se não usar --url)")
    ap.add_argument("--producer", default=None, help="ID da produtora (descobre sozinho se omitido)")
    ap.add_argument("--info", action="store_true", help="mostra os dados do evento e sai")
    ap.add_argument("--api-base", default=DEFAULT_API, help="base da API (para testes locais)")
    ap.add_argument("--workers", type=int, default=12,
                    help="requisições simultâneas (padrão 12; acima de ~40 a Cloudflare bloqueia)")
    ap.add_argument("--timeout", type=int, default=20, help="timeout por requisição (s)")
    ap.add_argument("--retries", type=int, default=2, help="tentativas extras para falhas de rede")
    ap.add_argument("--chunk", type=int, default=150, help="códigos por lote do curl")
    ap.add_argument("--pause", type=float, default=1.0, help="pausa entre lotes (s)")
    ap.add_argument("--cache", default=".cache_cupons.jsonl", help="cache/retomada (JSONL)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--retest", action="store_true", help="reprocessa códigos já cacheados")
    ap.add_argument("--out-csv", default="resultados.csv")
    ap.add_argument("--out-json", default="resultados.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    log = (lambda *a: None) if args.quiet else print

    # ------------------------------------------------------ evento e produtora
    event = extrair_event_id(args.url) if args.url else (args.event or DEFAULT_EVENT)

    info = None
    if args.info or not args.producer:
        info = descobrir_evento(event, args.api_base, args.timeout)
        if info:
            log(f"evento: {info['name']} ({info['place'] or 'local não informado'})")
            log(f"produtora: {info['producer_name']} — {info['producer_id']}")
            log(f"aceita cupom: {'sim' if info['has_coupon'] else 'não informado'} | "
                f"pede passkey: {'sim' if info['has_passkey'] else 'não'}")
        elif args.info:
            sys.exit("não consegui consultar o evento: confira o ID na URL do carrinho "
                     "(cart.ingresse.com/<ID>/tickets) ou se a API está bloqueando.")

    if args.info:
        sys.exit(0)

    producer = args.producer or (info or {}).get("producer_id") or DEFAULT_PRODUCER
    if not args.producer and info and not info.get("producer_id"):
        log("aviso: não descobri o producer_id; usando o padrão do projeto "
            "(informe com --producer se o evento for de outra produtora)")

    url = f"{args.api_base.rstrip('/')}/v1/events/{event}/tickets"
    t0 = time.time()

    codigos = carregar_codigos(args.listas)
    if not codigos:
        sys.exit("nenhum código válido nas listas")

    cache_path = Path(args.cache)
    cache = {} if (args.no_cache or args.retest) else carregar_cache(cache_path)
    pendentes = [c for c in codigos if c not in cache]

    log(f"códigos: {len(codigos)} | já testados: {len(codigos) - len(pendentes)} | a testar: {len(pendentes)}")

    resultados = dict(cache)
    falhas, bloqueado = [], None
    workers = max(1, args.workers)
    fcache = None
    if pendentes and not args.no_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fcache = cache_path.open("a", encoding="utf-8")

    def gravar(reg):
        resultados[reg["code"]] = reg
        if fcache:
            fcache.write(json.dumps(reg, ensure_ascii=False) + "\n")

    try:
        processados = 0
        for inicio in range(0, len(pendentes), args.chunk):
            lote = pendentes[inicio:inicio + args.chunk]
            respostas, retry_after = rodar_lote(lote, url, event, producer, workers, args.timeout)

            bloqueio_lote = False
            for c in lote:
                st, corpo = respostas.get(c, (0, None))
                reg, tipo = interpretar(c, st, corpo)
                if tipo == "rate":
                    falhas.append(c)
                    bloqueio_lote = True
                elif tipo == "retry":
                    falhas.append(c)
                else:
                    gravar(reg)
            if fcache:
                fcache.flush()

            if bloqueio_lote:
                bloqueado = retry_after or 1800
                log(f"\n  bloqueio por rate limit (HTTP 429). Liberação estimada em "
                    f"{bloqueado // 60} min. Progresso salvo no cache.")
                break

            processados += len(lote)
            if not args.quiet:
                decorrido = time.time() - t0
                vel = processados / decorrido if decorrido else 0
                eta = (len(pendentes) - processados) / vel if vel else 0
                print(f"\r  {processados}/{len(pendentes)} ({vel:.0f}/s, ETA {eta:.0f}s)", end="", flush=True)

            # concorrência adaptativa: se um quarto do lote falhar, reduz
            if len(lote) and sum(1 for c in lote if c in falhas) / len(lote) > 0.25 and workers > 4:
                workers = max(4, workers // 2)
                log(f"\n  muitas falhas: reduzindo para {workers} simultâneas")
            if args.pause:
                time.sleep(args.pause)
        if pendentes and not args.quiet:
            print()
    finally:
        if fcache:
            fcache.close()

    # retries de falhas de rede (sem insistir se houve bloqueio)
    for tentativa in range(1, args.retries + 1):
        if not falhas or bloqueado:
            break
        time.sleep(min(2 ** tentativa, 8))
        lote, falhas = falhas, []
        log(f"  retry {tentativa}: {len(lote)} códigos")
        respostas, retry_after = rodar_lote(lote, url, event, producer, max(4, workers // 2), args.timeout)
        for c in lote:
            st, corpo = respostas.get(c, (0, None))
            reg, tipo = interpretar(c, st, corpo)
            if tipo != "ok":
                falhas.append(c)
                if tipo == "rate":
                    bloqueado = retry_after or 1800
                continue
            resultados[c] = reg
            if not args.no_cache:
                with cache_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(reg, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- saídas
    ordem = {c: i for i, c in enumerate(codigos)}
    finais = [resultados[c] for c in codigos if c in resultados]
    finais.sort(key=lambda r: ordem[r["code"]])

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(finais, f, ensure_ascii=False, indent=1)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "valido", "desconto_pct", "tipo", "usos", "max_usos",
                    "max_por_usuario", "inicio", "fim", "status", "mensagem"])
        for r in finais:
            w.writerow([r["code"], "SIM" if r["valid"] else "nao", r.get("discount", ""),
                        r.get("type", ""), r.get("usages", ""), r.get("max_usages", ""),
                        r.get("per_user", ""), r.get("start", ""), r.get("end", ""),
                        r.get("status", ""), r.get("msg", "")])

    validos = [r for r in finais if r["valid"]]
    decorrido = time.time() - t0
    print("\n=== resumo ===")
    print(f"testados agora: {processados} | na saída: {len(finais)} | falhas: {len(falhas)}")
    if decorrido and processados:
        print(f"tempo: {decorrido:.1f}s | velocidade: {processados / decorrido:.1f} códigos/s")
    print(f"válidos: {len(validos)}")
    for v in sorted(validos, key=lambda r: -(r.get("discount") or 0)):
        print(f"  {v['code']}: {v.get('discount')}% {v.get('type') or ''} "
              f"(usos {v.get('usages')}/{v.get('max_usages') or '∞'})")
    if falhas:
        print("não concluídos:", ", ".join(falhas[:15]) + (" ..." if len(falhas) > 15 else ""))
    print(f"arquivos: {args.out_csv} e {args.out_json}")
    if bloqueado:
        print(f"ATENÇÃO: API bloqueou por excesso de requisições; tente novamente em ~{bloqueado // 60} min.")
        sys.exit(3)
    sys.exit(2 if falhas else 0)


if __name__ == "__main__":
    main()
