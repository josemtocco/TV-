# TV+

Agregador brasileiro de canais M3U otimizado para **SS IPTV**.

## O que o projeto faz

- Consulta as 15 fontes M3U configuradas em `fontes.txt`.
- Junta os canais encontrados.
- Remove entradas do tipo YouTube/página web que não são streams diretos.
- Remove duplicatas pelo URL exato do stream.
- Normaliza nomes de canais.
- Junta categorias equivalentes em categorias comuns.
- Mantém `tvg-id`, `tvg-name`, `tvg-logo` e `group-title` quando disponíveis.
- Usa EPG nacional como fonte global:
  `https://raw.githubusercontent.com/iptv-com/epg/main/guides/brazil.xml`
- Testa os streams em paralelo para não tornar a execução excessivamente lenta.
- Atualiza automaticamente a cada 6 horas pelo GitHub Actions.
- **Não apaga um canal já validado por causa de uma falha temporária.**
- Um canal precisa falhar em 3 atualizações consecutivas para ser removido.
- Se uma fonte inteira ficar temporariamente indisponível, os canais previamente validados daquela fonte entram na janela de proteção.
- A saída é `TV+.m3u`, pronta para uso no SS IPTV.

## Arquivo para o SS IPTV

Depois do primeiro GitHub Actions bem-sucedido:

`https://raw.githubusercontent.com/SEU_USUARIO/TV-/main/TV%2B.m3u`

O nome do repositório recomendado é **TV+**.

## Atualização

O workflow roda a cada 6 horas e também pode ser iniciado manualmente em:

`Actions -> TV+ - atualizar M3U -> Run workflow`

## Proteção contra perda de canais

A regra é deliberadamente conservadora:

1. Canal novo + stream válido: entra.
2. Canal já existente + stream válido: permanece e atualiza metadados.
3. Canal já existente + falha temporária: permanece.
4. Três falhas consecutivas: é considerado inativo e pode ser removido.
5. Fonte inteira fora do ar: os canais já conhecidos não são apagados imediatamente.
6. URL idêntica: somente uma entrada é mantida.

Isso evita que uma instabilidade de GitHub, CDN, servidor HLS ou fonte M3U destrua a playlist.

## Observação sobre EPG

O `x-tvg-url` aponta para o guia brasileiro do projeto IPTV-com. O preenchimento efetivo da programação depende de existir correspondência entre o `tvg-id` do canal e os IDs presentes no XMLTV. Quando uma fonte já fornece `tvg-id`, ele é preservado.

## Arquivos

- `gerar_m3u.py` — agregação, normalização, validação, deduplicação e geração.
- `fontes.txt` — as 15 fontes solicitadas.
- `TV+.m3u` — playlist final gerada.
- `tvplus_state.json` — histórico para a proteção contra remoção prematura.
- `.github/workflows/update.yml` — atualização automática a cada 6 horas.
