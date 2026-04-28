# Multi-agent Code Reviewer

Praktická ukázka **multi-agent orchestrace** postavená na **Claude Agent SDK
pro Python**. Implementuje vzor **Supervisor + Parallel**.

Odevzdávka pro kurz **Vibe Coding 1** (Global Classes CZE,
[Vibe-Coding-1](https://github.com/Global-Classes-CZE/Vibe-Coding-1)).

---

## Co to dělá

Vezme kód (soubor, git diff, nebo stdin) a pustí na něj **tři specialisty
paralelně** přes `asyncio.gather`:

- **Bezpečnostní reviewer** — SQL injection, hardcoded credentials, slabé hashe…
- **Výkonnostní reviewer** — N+1 dotazy, blokující I/O, neefektivní algoritmy…
- **Stylový reviewer** — naming, dlouhé funkce, magic numbers, hloubka vnoření…

Pak je spojí **supervisor**, který je sloučí, deduplikuje a vrátí jeden
strukturovaný report se skóre 1–10 a TL;DR.

```
                          ┌──────────────────┐
            user code ──► │   review_code()  │ ──► final markdown report
                          └────────┬─────────┘
                                   │
                       asyncio.gather (paralelně)
                       ┌───────────┼────────────┐
                       ▼           ▼            ▼
                  ┌─────────┐ ┌─────────┐ ┌──────────┐
                  │Security │ │Perform. │ │ Style    │   Haiku 4.5
                  │reviewer │ │reviewer │ │ reviewer │   (rychlý+levný)
                  └────┬────┘ └────┬────┘ └────┬─────┘
                       └───────────┼───────────┘
                                   ▼
                          ┌──────────────────┐
                          │   Supervisor     │   Sonnet 4.6
                          │   (consolidace)  │   (silnější syntéza)
                          └──────────────────┘
```

## Jaké orchestrační vzory ukazuje

Z výčtu v zadání:

| Vzor v zadání | Použito? | Kde                                                  |
|---------------|----------|------------------------------------------------------|
| Sequential    | ✅       | Specialisté → Supervisor je striktně sekvenční fáze  |
| **Parallel**  | ✅       | `asyncio.gather()` na 3 specialistech současně       |
| Loop          | ❌       | (nebylo potřeba pro tenhle use case)                  |
| Conditional   | ❌       | (mohlo by se přidat — např. "skip security pokud diff je jen test")|
| Collaboration | ➖       | Specialisté nespolupracují, ale jejich výstupy se slévají |
| **Supervisor**| ✅       | Supervisor řídí, slévá, finalizuje report            |
| Swarm         | ❌       | Pro 3 fixní role je swarm overkill                   |

Hlavní vzor: **Supervisor + Parallel** — supervisor dispatchne specialisty,
ti běží paralelně, supervisor slije výstupy.

## Klíčové nápady, které stojí za zdůraznění

1. **Heterogenní modely.** Specialisté používají rychlé/levné `claude-haiku-4-5`,
   protože jejich úkol je úzce specializovaný. Supervisor používá silnější
   `claude-sonnet-4-5`, protože syntéza vyžaduje lepší reasoning. Cca **5×
   levnější** než pustit všechny agenty na Sonnet.

2. **Strukturované prompty.** Každý specialista dostává v promptu **přesnou
   markdown šablonu**. Bez ní by každý vrátil jiný formát a supervisor by
   musel napřed parsovat. Takhle je sloučení mechanické.

3. **Bez nástrojů.** `allowed_tools=[]` u všech agentů — analýza textu nepotřebuje
   Read/Bash, jen prompt + odpověď. Rychlejší, levnější, deterministi(č)tější.

4. **Měření.** Po každém běhu vidíš kolik trval každý agent a kolik stál
   v dolarech. `FinalReview.total_duration_s` bere maximum specialistů (kritická
   cesta paralelního běhu) + supervisora.

---

## Instalace

```bash
git clone https://github.com/dotexocz/code-reviewer-agent.git
cd code-reviewer-agent

# Vytvoř virtuální prostředí (doporučeno)
python3 -m venv venv
source venv/bin/activate

# Nainstaluj závislosti
pip3 install -r requirements.txt
```

### Autentizace

SDK potřebuje přístup ke Claude. Dvě cesty:

1. **Máš Claude Code** (`claude` na PATH): SDK použije tvoji přihlašovací
   kontextu automaticky. Nic dalšího nepotřebuješ.
2. **API klíč:** Pokud Claude Code nemáš, zkopíruj `.env.example` na `.env`
   a vyplň `ANTHROPIC_API_KEY=sk-ant-…` (klíč vytvoříš na
   <https://console.anthropic.com/settings/keys>).

---

## Použití

### Web UI (pro klikací demo)

```bash
python3 -m reviewer.web
# → otevři http://127.0.0.1:8000/
```

V prohlížeči vlož kód do textarea, klikni **Spustit review** a uvidíš
strukturovaný report s tabulkou statistik. Cmd/Ctrl+Enter slouží jako
zkratka pro odeslání.

Tlačítka „SQL injection", „N+1 dotaz" a „Čistý kód" rovnou nahodí ukázkové
příklady.

### Review konkrétního souboru (CLI)

```bash
python3 -m reviewer examples/vulnerable_login.py
```

### Review aktuálních neuložených změn (git diff)

```bash
python3 -m reviewer --diff
```

### Review ze stdin

```bash
cat any_file.py | python3 -m reviewer -
```

### Uložit finální report do souboru

```bash
python3 -m reviewer examples/vulnerable_login.py --output reports/login.md
```

### Strojový JSON výstup

```bash
python3 -m reviewer examples/vulnerable_login.py --json > review.json
```

### Help

```bash
python3 -m reviewer --help
```

---

## Příklad výstupu

Spuštění nad `examples/vulnerable_login.py` (úmyslně chybový kód) vypadá
zhruba takto:

```
╭────── Multi-agent code reviewer ──────╮
│  Cíl review: examples/vulnerable_login.py
│  3 specialisté běží paralelně → supervisor je sloučí.
╰───────────────────────────────────────╯

# Code review

**Skóre:** 2/10 — kód má více kritických bezpečnostních zranitelností,
N+1 výkonnostní problémy a porušuje základní stylové konvence.

## TL;DR — co opravit teď

- 🔴 SQL injection v `login()` a `render_user_profile()` — nahradit
  parametrizovanými dotazy (`?` placeholdery).
- 🟠 N+1 dotaz v `get_orders_for_users()` — sloučit do jednoho SELECT
  s `WHERE user_id IN (...)`.
- 🟡 Hluboké vnoření v `process_data()` (5 úrovní) — extrahovat do
  pomocných funkcí.

(...detailní nálezy a statistiky...)

  Statistika běhu
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┓
┃ Agent                   ┃  Délka ┃ Cena USD┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━┩
│ Bezpečnostní reviewer   │  6.2 s │ $0.0084 │
│ Výkonnostní reviewer    │  5.8 s │ $0.0072 │
│ Stylový reviewer        │  6.1 s │ $0.0078 │
│ Supervisor              │  4.3 s │ $0.0156 │
├─────────────────────────┼────────┼─────────┤
│ Celkem (kritická cesta) │ 10.5 s │ $0.0390 │
└─────────────────────────┴────────┴─────────┘
```

> Číslo "Celkem" bere **maximum** specialistů (kritická cesta paralelního
> běhu) + supervisora. Při sekvenčním řešení by tu bylo `6.2 + 5.8 + 6.1 + 4.3 = 22.4 s`,
> takže paralelizace ušetří ~50 % času.

---

## Struktura kódu

```
code-reviewer-agent/
├── README.md                        ← tento soubor
├── requirements.txt
├── .env.example
├── .gitignore
├── reviewer/
│   ├── __init__.py                  ← package metadata + diagram
│   ├── __main__.py                  ← CLI (python -m reviewer)
│   ├── orchestrator.py              ← Supervisor + Parallel logika
│   ├── prompts.py                   ← system prompty pro 4 agenty
│   ├── web.py                       ← FastAPI web UI (python -m reviewer.web)
│   └── static/
│       └── index.html               ← dark UI s textarea a vykreslením reportu
├── examples/
│   └── vulnerable_login.py          ← úmyslně chybový kód pro demo
└── docs/
    └── architecture.md              ← detailní popis vzoru
```

Pro hlubší pohled do toho, **proč** je to napsané tak, jak je, viz
[`docs/architecture.md`](docs/architecture.md).

---

## Co by se dalo přidat (roadmap)

- **GitHub PR mode** (`--pr <url>`) — review celého PR přes `gh` CLI nebo
  GitHub MCP server.
- **Loop pattern** — pokud supervisor dostane skóre < 5, znovu pošle
  specialistům s instrukcí jít hlouběji.
- **Conditional pattern** — preflighter, který podle typu souboru rozhodne,
  zda vůbec spouštět security reviewera (např. u `*.md` ne).
- **Caching** — pokud se kód nezměnil, vrátit poslední výsledek.
- **Více jazyků** — teď je výstup v češtině; přepínač `--lang en/cs`.

---

## Licence a autor

Lukáš Melichar, 04/2026 — kurz Vibe Coding 1.

Zdrojový kód volně k dispozici jako odevzdávka — bez konkrétní licence.
