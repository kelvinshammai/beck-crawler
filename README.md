# beck-crawler

Skill do [Claude Code](https://claude.com/claude-code) que consulta
disponibilidade e preço de produtos em bots de pacientes de associações
cannábicas construídos sobre [Typebot](https://typebot.io/).

## Pré-requisitos

- [Claude Code](https://claude.com/claude-code) instalado.
- Python 3 com a lib `requests` (`pip install requests`) — usado pelo método
  preferido (API pública do Typebot).
- Opcionalmente, `Pillow` (`pip install Pillow`) — só necessário se for usar
  `crawler.crop_two_panel_image()` ao gerar a página HTML do catálogo (ver
  abaixo).
- Opcionalmente, a **extensão Claude no Chrome** instalada e com permissão no
  site do bot da sua associação, pro método alternativo via navegador
  (usado só se a API não funcionar ou pra descobrir o fluxo de uma
  associação nova).

## Instalar

Este repo é um **plugin** do Claude Code (tem `.claude-plugin/plugin.json` +
`.claude-plugin/marketplace.json` na raiz, listando a si mesmo). Dentro do
Claude Code:

```
/plugin marketplace add kelvinshammai/beck-crawler
/plugin install beck-crawler@beck-crawler-marketplace
```

Se preferir sem plugin/GitHub — clone e copie a pasta da skill manualmente,
funciona igual (o Claude Code só olha o conteúdo de `.claude/skills/<nome>/`,
não liga pra como ela chegou lá):

```bash
git clone https://github.com/kelvinshammai/beck-crawler.git
cp -r beck-crawler/skills/update-strains /caminho/do/seu/projeto/.claude/skills/
```

Ou, pra deixar disponível em **todos** os seus projetos:

```bash
cp -r beck-crawler/skills/update-strains ~/.claude/skills/
```

## Usar

Dentro do Claude Code, no projeto onde a skill foi instalada:

```
/update-strains abecmed <seu-cpf>
```

Cada execução dispara **dois agentes**: um rápido, que só consulta preço/
disponibilidade (sem clicar em nenhum produto) e responde direto no chat; e
um em segundo plano, que clica em cada produto pra pegar foto + ficha
técnica e gera/atualiza a página HTML do catálogo no projeto atual — você
recebe a resposta rápida primeiro e o aviso do catálogo pronto depois,
sem precisar esperar um pelo outro.

O primeiro argumento é o identificador da associação (chave ou nome em
`associations.json`, sem diferenciar maiúsculas/acentos). O CPF é sempre
pedido explicitamente — a skill nunca reusa ou inventa um CPF.

Se a associação ainda não estiver cadastrada, a skill vai pedir a URL do bot
dela, tentar descobrir o fluxo, e (se funcionar) propor adicionar ao
registro.

## Contribuir uma associação nova

Depois de confirmar que o fluxo funciona pra uma associação nova (via
`/update-strains`, que já guia esse processo), edite
`skills/update-strains/associations.json` adicionando a entrada
descoberta e abra um PR. **Nunca** inclua CPF ou qualquer dado de paciente
nesse arquivo — só configuração do bot (URL, slug, dicas de texto dos
botões).

## A página HTML do catálogo

A skill monta uma página HTML de catálogo (cards, filtros, busca, modal de
detalhe) genérica pra qualquer associação, gerada automaticamente (agente 2,
ver acima) em todo `/update-strains`. Veja a seção "Agent 2: build/update
the HTML catalog" em `skills/update-strains/SKILL.md` para o passo a passo;
a página fica no projeto onde a skill é rodada (não neste repo), em algo
como `catalogo-<associacao>/index.html`, e segue uma política never-delete/
mark-availability (produto nunca some do catálogo, só é marcado como
indisponível), implementada em código (`catalog.py`) em vez de edição
manual do HTML.

## Rodar sem o Claude (linha de comando)

`crawler.py`/`catalog.py` são Python puro (só `requests`, e `Pillow` se
quiser o recorte de foto) — não precisam do Claude Code pra funcionar.
`cli.py` amarra os dois num comando único, sem precisar de agente nenhum:

```bash
cd skills/update-strains
python3 cli.py abecmed <seu-cpf> [caminho/do/catalogo.html]
```

Isso consulta o bot uma vez, imprime a lista de preços no terminal, e já
monta/atualiza a página HTML do catálogo (recortando a foto em foto+ficha
técnica automaticamente pras associações marcadas como
`"photo_template": "two_panel"` no `associations.json`, e extraindo
genética/THC/terpenos do texto da descrição via regex). Diferenças pro fluxo
via Claude Code:

- É um processo único e sequencial (preço só imprime depois que a captura
  inteira termina) — não tem o "resposta rápida agora, catálogo depois" dos
  dois agentes.
- Não faz descoberta interativa de associação nova — se `<associacao>` não
  estiver em `associations.json`, o script só avisa e para; adicione a
  entrada manualmente primeiro (ver "Unknown association" no `SKILL.md`).
- Não lê a foto visualmente, então CBD%/moisture/water-activity (que só
  existem na imagem da ficha técnica, nunca no texto do bot) ficam vazios.
  Pra isso, precisa rodar via Claude Code mesmo.

## Limitações conhecidas

- Cada bot de associação é construído e mantido de forma independente; o
  fluxo pode mudar sem aviso. Se uma associação que já funcionava parar de
  funcionar, trate como associação nova (redescubra o fluxo) em vez de supor
  que os `flow_hints` salvos ainda são válidos.
- A geração da página rica (com foto/THC/CBD por produto) faz a skill clicar
  em produtos individuais no bot — algo que a checagem de preço evita
  propositalmente. Isso roda em segundo plano (agente 2) com as travas de
  segurança descritas no `SKILL.md`; se ele parar por segurança, o produto
  fica sem foto até uma próxima execução, mas o catálogo em si nunca fica
  corrompido.
