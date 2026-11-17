#!/bin/bash
# Roda uma lista grande de cupons com retomada automática após rate limit.
#
# uso:
#   ./rodar_grande.sh lista.txt "https://cart.ingresse.com/<EVENT_ID>/tickets" [workers]
#
# o que faz:
#   - chama o testar_cupons.py em loop até a lista terminar
#   - se a API bloquear (saída 3), espera e continua de onde parou (usa o cache)
#   - se houver falha de rede (saída 2), tenta de novo
#   - se o evento não tiver cupom (saída 4), para
#   - grava tudo em log com horário e, no fim, mostra os válidos

set -u

LISTA="${1:?informe o arquivo da lista}"
URL="${2:?informe a URL do carrinho}"
WORKERS="${3:-20}"

BASE="$(cd "$(dirname "$0")" && pwd)"
NOME="$(basename "$LISTA" .txt)"
LOG="$BASE/$NOME.log"
ESPERA_BLOQUEIO=900      # 15 min quando a API bloqueia
ESPERA_FALHA=60          # 1 min quando dá falha de rede
MAX_TENTATIVAS=40

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

log "lista: $LISTA ($(wc -l < "$LISTA") códigos) | workers: $WORKERS"
log "url: $URL"

for i in $(seq 1 "$MAX_TENTATIVAS"); do
    log "--- tentativa $i"
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

# resumo final a partir do JSON
if [ -f "$BASE/$NOME.json" ]; then
    python3 - "$BASE/$NOME.json" <<'PY'
import json, sys
dados = json.load(open(sys.argv[1]))
validos = [d for d in dados if d.get("valid")]
print(f"\ntotal na lista: {len(dados)} | válidos: {len(validos)}")
for d in validos:
    print(f"  {d['code']}: {d.get('discount')}% ({d.get('type')}) usos {d.get('usages')}/{d.get('max_usages')}")
PY
fi
