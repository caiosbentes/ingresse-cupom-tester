#!/bin/bash
# Roda uma lista grande de cupons com retomada automática após rate limit.
# ---8<--- ajuda (bloco mostrado por --help; não remova os marcadores)
# uso:
#   ./rodar_grande.sh lista.txt "https://cart.ingresse.com/<EVENT_ID>/tickets" [workers]
#
# o que faz:
#   - valida os argumentos e o ambiente antes de começar
#   - chama o testar_cupons.py em loop até a lista terminar
#   - se a API bloquear (saída 3), espera e continua de onde parou (usa o cache)
#   - se houver falha de rede (saída 2), tenta de novo
#   - se o evento não tiver cupom (saída 4), para
#   - grava tudo em log com data/hora e, no fim, mostra os válidos
# ---8<--- fim da ajuda

set -u

erro() { echo "erro: $*" >&2; echo "      (veja o uso: ./rodar_grande.sh --help)" >&2; exit 1; }

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    # imprime o bloco entre os marcadores ---8<--- , sem depender de números de linha
    sed -n '/^# ---8<--- ajuda/,/^# ---8<--- fim da ajuda/p' "$0" \
        | sed '1d;$d' | sed 's/^# \{0,1\}//'
    exit 0
fi

[ $# -ge 2 ] || erro "faltam argumentos. use: ./rodar_grande.sh lista.txt \"https://cart.ingresse.com/<EVENT_ID>/tickets\" [workers]"

LISTA="$1"
URL="$2"
WORKERS="${3:-20}"

BASE="$(cd "$(dirname "$0")" && pwd)"
NOME="$(basename "$LISTA" .txt)"
LOG="$BASE/$NOME.log"

# ---------------------------------------------------------------- checagens
[ -f "$LISTA" ] || erro "arquivo não encontrado: $LISTA (rode na pasta onde está a lista)"
[ -f "$BASE/testar_cupons.py" ] || erro "testar_cupons.py não está em $BASE"
command -v python3 >/dev/null 2>&1 || erro "python3 não encontrado no PATH"
command -v curl >/dev/null 2>&1 || erro "curl não encontrado no PATH"

# workers precisa ser um inteiro positivo (vai direto pro --workers do Python)
case "$WORKERS" in
    ''|*[!0-9]*) erro "workers inválido: '$WORKERS' (use um número inteiro, ex.: 20)" ;;
esac
[ "$WORKERS" -ge 1 ] || erro "workers precisa ser >= 1 (recebi '$WORKERS')"

# curl precisa de --parallel (7.68+)
if ! curl --help all 2>/dev/null | grep -q -- '--parallel'; then
    echo "aviso: seu curl não tem --parallel (versão antiga). o teste vai ficar lento." >&2
    echo "       no Ubuntu/Debian: sudo apt install curl" >&2
fi

# URL precisa ter o ID do evento
case "$URL" in
    *"<EVENT_ID>"*|*"EVENT_ID"*) erro "troque <EVENT_ID> pelo ID real do evento (o trecho da URL do carrinho)" ;;
    *"://"*) ;;
    *) erro "faltou a URL do carrinho. use: ./rodar_grande.sh \"$LISTA\" \"https://cart.ingresse.com/<EVENT_ID>/tickets\" [workers]" ;;
esac
case "$URL" in
    *cart.ingresse.com/*) ;;
    *) echo "aviso: a URL não parece de carrinho da Ingresse: $URL" >&2 ;;
esac

# arquivo com CRLF (salvo no Windows) quebra o bash
if head -1 "$BASE/rodar_grande.sh" | grep -q $'\r'; then
    erro "o script está com terminações de linha do Windows (CRLF). rode: sed -i 's/\\r$//' \"$0\""
fi

# log precisa ser gravável
if ! touch "$LOG" 2>/dev/null; then
    erro "não consegui criar o log em $LOG (sem permissão de escrita?)"
fi

ESPERA_BLOQUEIO=900      # 15 min quando a API bloqueia
ESPERA_FALHA=60          # 1 min quando dá falha de rede
MAX_TENTATIVAS=40

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# Ctrl+C / kill: registra no log que foi interrompido (o cache preserva o progresso)
trap 'log "interrompido manualmente — o progresso está salvo no cache, rode de novo para retomar"; exit 130' INT TERM

log "lista: $LISTA ($(wc -l < "$LISTA") códigos) | workers: $WORKERS"
log "url: $URL"
log "log: $LOG"

concluiu=0
for i in $(seq 1 "$MAX_TENTATIVAS"); do
    log "--- tentativa $i de $MAX_TENTATIVAS"
    python3 "$BASE/testar_cupons.py" "$LISTA" \
        --url "$URL" \
        --workers "$WORKERS" \
        --pause 1.5 \
        --retries 2 \
        --out-csv "$BASE/$NOME.csv" \
        --out-json "$BASE/$NOME.json" 2>&1 | tee -a "$LOG"
    saida=${PIPESTATUS[0]}

    case "$saida" in
        0)
            log "concluído"
            concluiu=1
            break
            ;;
        3)
            log "API bloqueou. aguardando $((ESPERA_BLOQUEIO / 60)) min e retomando..."
            sleep "$ESPERA_BLOQUEIO"
            ;;
        2)
            log "falhas de rede. aguardando $((ESPERA_FALHA / 60)) min e retomando..."
            sleep "$ESPERA_FALHA"
            ;;
        4)
            log "evento sem cupons cadastrados: encerrando"
            exit 4
            ;;
        *)
            log "erro inesperado (saída $saida): encerrando"
            exit "$saida"
            ;;
    esac
done

if [ "$concluiu" -eq 0 ]; then
    log "atenção: esgotei as $MAX_TENTATIVAS tentativas sem terminar a lista."
    log "         o progresso está salvo no cache — rode o comando de novo para continuar,"
    log "         ou aumente MAX_TENTATIVAS no topo do script."
fi

# resumo final a partir do JSON
if [ -f "$BASE/$NOME.json" ]; then
    python3 - "$BASE/$NOME.json" <<'PY' | tee -a "$LOG"
import json, sys
dados = json.load(open(sys.argv[1]))
validos = [d for d in dados if d.get("valid")]
print(f"\ntotal na lista: {len(dados)} | válidos: {len(validos)}")
for d in validos:
    print(f"  {d['code']}: {d.get('discount')}% ({d.get('type')}) usos {d.get('usages')}/{d.get('max_usages')}")
PY
fi

# saída não-zero se não terminou, para quem encadeia o script perceber
[ "$concluiu" -eq 1 ] || exit 2
