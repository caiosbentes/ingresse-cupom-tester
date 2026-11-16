# Testador de cupons — checkout Ingresse

Script de linha de comando que testa uma lista de códigos de cupom no checkout da
[Ingresse](https://www.ingresse.com/) e diz quais são válidos, com qual desconto e
quais são as regras de uso.

Foi feito para responder a uma pergunta simples: *"existe um cupom melhor que esse
que a produtora divulgou?"* Em vez de digitar código por código no site, o script
dispara a mesma validação que a página faz, em paralelo, e devolve uma tabela.

```
$ python3 testar_cupons.py cupons.txt

códigos: 500 | já testados: 0 | a testar: 500
  500/500 (29/s, ETA 0s)

=== resumo ===
testados agora: 500 | na saída: 500 | falhas: 0
tempo: 17.2s | velocidade: 29.1 códigos/s
válidos: 2
  ROMAEVENTOS: 50% percentage (usos 2/100)
  HYPE24HSINTRO: 30% percentage (usos 16/∞)
arquivos: resultados.csv e resultados.json
```

## Como funciona

O checkout (`cart.ingresse.com`) valida o cupom com uma chamada própria:

```
POST https://apigw-purchase.prod.ingresse.com/v1/events/{evento}/tickets
Content-Type: application/json
Origin: https://cart.ingresse.com

{
  "event_id": "...", "producer_id": "...", "providers": [],
  "user_document": "", "user_id": "", "code": "ROMAEVENTOS"
}
```

Se o código existe, a resposta traz um objeto `data.coupon` com `discount_amount`,
`discount_type`, `max_usages`, `max_uses_per_user`, `start_date` e `end_date`.
Se não existe, vem `data.extras.code = "BFRULE020"` com a mensagem
*"Passkey does not match any available ticket"*.

O script reproduz essa chamada. O detalhe de performance é **como** ela é feita:

```
sem otimização                      com otimização
--------------                      ---------------
código → processo curl → TLS 1,2 s  códigos → 1 processo curl (--parallel)
       → request → resposta                  → 1 conexão TLS reaproveitada
       → repete 500 vezes                    → HTTP/2 multiplexado + gzip
                                            → 500 respostas em ~17 s
```

Medições na mesma lista de 503 códigos, contra a API real:

| Configuração | Tempo | Velocidade |
|---|---|---|
| 1 processo `curl` por código, 4 threads | 286 s | 1,7 códigos/s |
| Lote paralelo, 30 simultâneas | 16,7 s | 30 códigos/s |
| Lote paralelo, 40 simultâneas | 20,7 s | 24 códigos/s |
| Lote paralelo, 60 simultâneas | 117 s | 4,3 códigos/s |

Acima de ~40 simultâneas o servidor começa a enfileirar e a taxa cai. A faixa
boa é 20–30.

## Requisitos

- Python 3.8+ (só biblioteca padrão, sem `pip install`)
- `curl` 7.66+ com suporte a HTTP/2 (`curl --version` deve mostrar `HTTP2`)

## Instalação

```bash
git clone https://github.com/<seu-usuario>/ingresse-cupom-tester.git
cd ingresse-cupom-tester
chmod +x testar_cupons.py
```

## Uso

Lista em texto, um código por linha (também aceita separados por vírgula ou ponto e vírgula):

```bash
python3 testar_cupons.py exemplos/cupons_exemplo.txt
```

Várias listas de uma vez, com saídas nomeadas:

```bash
python3 testar_cupons.py lista1.txt lista2.txt --workers 30 --out-csv r.csv --out-json r.json
```

Outro evento (o ID está na URL do carrinho: `cart.ingresse.com/<EVENT_ID>/tickets`):

```bash
python3 testar_cupons.py cupons.txt --event OUTRO_ID --producer OUTRO_ID
```

### Opções

| Opção | Padrão | Descrição |
|---|---|---|
| `--workers` | `12` | requisições simultâneas (20–30 é o ponto ideal) |
| `--chunk` | `150` | códigos por lote do `curl` |
| `--pause` | `1.0` | pausa entre lotes, em segundos |
| `--timeout` | `20` | timeout por requisição, em segundos |
| `--retries` | `2` | tentativas extras para falhas de rede |
| `--cache` | `.cache_cupons.jsonl` | arquivo de cache/retomada |
| `--no-cache` | — | não lê nem grava cache |
| `--retest` | — | reprocessa códigos já cacheados |
| `--event` / `--producer` | Intro Belém | IDs do evento e da produtora |
| `--api-base` | API da Ingresse | base alternativa (usada nos testes com mock) |
| `--out-csv` / `--out-json` | `resultados.csv` / `resultados.json` | arquivos de saída |
| `--quiet` | — | sem barra de progresso |

### Saídas

`resultados.csv`:

```csv
codigo,valido,desconto_pct,tipo,usos,max_usos,max_por_usuario,inicio,fim,status,mensagem
ROMAEVENTOS,SIM,50,percentage,2,100,2,,,active,válido
INTRO60,nao,,,,,,,,,Passkey does not match any available ticket
```

`resultados.json` traz os mesmos dados já tipados (números como número, datas como string ISO).

### Códigos de saída

| Código | Significado |
|---|---|
| `0` | tudo testado |
| `2` | alguma requisição não concluiu (rede/500) |
| `3` | API bloqueou por excesso de requisições (HTTP 429) |

## Cache e retomada

Cada resultado é gravado na hora em `.cache_cupons.jsonl`. Se o script cair, for
interrompido ou bater no rate limit, é só rodar de novo com a mesma lista: ele
pula o que já foi testado e continua do ponto onde parou. Para reprocessar tudo,
use `--retest`.

## Rate limit

A API fica atrás do Cloudflare. Em rajadas grandes ela responde:

```
HTTP 429
retry-after: 3174
error code: 1015
```

Quando isso acontece o script **para**, salva o progresso e sai com código 3,
informando o tempo estimado de liberação. Ele não insiste, porque insistir
prolonga o bloqueio. Se precisar de mais volume, aumente `--pause`, reduza
`--workers` e rode em horários diferentes.

## Testes

O repositório inclui um servidor falso (`mock_api.py`) que imita a validação:
responde válido para `ROMAEVENTOS` e `HYPE24HSINTRO`, inválido para o resto, 429
para códigos que começam com `RL` e 500 para os que começam com `ERR`. Assim dá
para validar mudanças sem gastar requisições reais.

```bash
python3 mock_api.py &                     # sobe em 127.0.0.1:8765
python3 testar_cupons.py exemplos/cupons_exemplo.txt \
    --api-base http://127.0.0.1:8765 --event teste --no-cache --workers 5
```

Saída esperada: os dois cupons válidos detectados, o resto inválido, código de
saída `0`.

## Limitações

- Cupons presos a um CPF ou conta específica não validam como visitante (a API
  aceita `user_document` e `user_id`, que o script envia vazios). Se a produção
  distribuiu um código nominal, ele pode funcionar logado mesmo dando inválido aqui.
- O script não compra nada: só consulta a validação do código.
- A API pode mudar sem aviso; se os resultados vierem todos "inválidos" de repente,
  confira com um código que você sabe que funciona.

## Aviso

Projeto para uso pessoal e educacional, para comparar cupons de um evento que você
pretende comprar. Respeite os termos de uso da plataforma e o rate limit. Não use
para revender, automatizar compras ou sobrecarregar o serviço.

## Licença

MIT — veja [LICENSE](LICENSE).
