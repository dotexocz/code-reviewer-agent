# Architektura: Supervisor + Parallel

Tento dokument vysvětluje, **proč** je multi-agent reviewer postavený zrovna
takhle a co každý kus kódu řeší.

## Vstupní motivace

Klasický code reviewer běží jako **jediný velký prompt**: pošleš modelu kód,
přidáš instrukce "podívej se na bezpečnost, výkon, styl" a doufáš, že nic
nevynechá.

Problémy s monolitem:

1. **Konflikt rolí.** Bezpečnostní review chce paranoiu ("co když je vstup
   zlomyslný?"). Stylový review chce zdravý rozum ("co je čitelné?").
   Když je to jeden agent, kompromisuje.
2. **Žádná paralelizace.** Kód se zpracovává jednou za sebou, latence
   se sčítá.
3. **Težko se škáluje.** Když chci přidat 4. specialistu (např. accessibility
   reviewera), musím ručně přepsat prompt.

## Multi-agent řešení

Místo jednoho promptu **rozdělím odpovědnost na 4 agenty**:

- 3 **specialisté** — každý má jeden úzký focus, žádné jiné nepokrývá.
- 1 **supervisor** — nezná detailní pravidla, jen **slévá** výstupy specialistů.

```
       ┌────────────────┐
       │   Supervisor   │
       └───────┬────────┘
               │
   ┌───────────┼───────────┐
   ▼           ▼           ▼
 Sec        Perf         Style
```

## Tok dat

```
review_code(code)
   │
   ├─ asyncio.gather(
   │     specialist("security", code),
   │     specialist("performance", code),
   │     specialist("style", code),
   │  )
   │  → tři SpecialistReport objekty (paralelně)
   │
   ├─ supervisor sloučí výstupy do jednoho promptu
   │  (každý dílčí report jako "## Report: <jméno>")
   │
   └─ supervisor zavolá ``query()`` se SUPERVISOR_PROMPT
      → finální markdown report
```

## Proč asyncio.gather, ne ThreadPoolExecutor

Claude Agent SDK je **async-native**. `query()` je `AsyncIterator`. V threadpoolu
bychom museli každé volání obalit do `asyncio.run`, což přidá overhead a
zruší výhodu sdíleného event loopu.

`asyncio.gather` je v tomhle případě nejjednodušší a nejrychlejší primitiv pro
paralelizaci — všechna 3 volání odešlou HTTP request současně, čekají vedle
sebe, vrátí výsledky najednou.

## Proč heterogenní modely

Specialisté dělají **úzce vymezenou analýzu textu**. To je úkol, na kterém
Haiku 4.5 zvládá:
- ~5× levnější
- ~2× rychlejší
- Stejně dobrý nebo lepší výstup, pokud je prompt jasný (a ten u nás je —
  s pevnou markdown šablonou)

Supervisor dělá **syntézu**. Musí:
- Pochopit tři různé reporty
- Najít, kde se překrývají
- Napsat top-of-mind shrnutí
- Ohodnotit skóre

Tady má Sonnet 4.5 znatelně lepší výstup než Haiku — vyplatí se zaplatit
~3× více tokenů, protože jde jen o jeden malý prompt na konci.

**Celková ekonomika** (rough numbers, závisí na velikosti kódu):

| Setup                            | Cena za review     | Latence    |
|----------------------------------|--------------------|------------|
| Vše Sonnet, sekvenčně             | $0.10              | ~24 s      |
| Vše Sonnet, paralelně             | $0.10              | ~10 s      |
| **Heterogenní + paralelně** (zde)| **$0.04**          | **~10 s**  |
| Vše Haiku, paralelně              | $0.02              | ~6 s       |

Heterogenní setup je **sweet spot** — 60 % úspora oproti vše-Sonnet při
zachování kvality syntézy.

## Proč žádné nástroje

Specialisté ani supervisor nepotřebují `Read`, `Write`, `Bash`. Veškerý
kontext (kód) jim přijde v promptu, výstup je markdown text. Když dáme
`allowed_tools=[]`, model **fyzicky nemůže** zavolat tool — což znamená:

- Žádný permission prompt
- Deterministi(č)tější chování
- Rychlejší (model nemarní turn na "hmm, mám zavolat tool?")

Pro tento usecase je čistá analýza textu plně postačující.

## Důsledky strukturovaných promptů

V `prompts.py` mám pro každého specialistu **přesnou markdown šablonu**.
Třeba pro `SECURITY_PROMPT`:

```
#### [SEVERITY] Krátký název nálezu
- **Kde:** `<soubor>:<řádek>`
- **Problém:** ...
- **Dopad:** ...
- **Oprava:** ...
```

Supervisor pak může spoléhat na to, že každý nález vypadá v této struktuře,
a když má sloučit duplicity ("dva reviewery našli SQL injection na stejném
řádku"), nemusí parsovat různé jazyky/formáty.

Bez šablon by supervisor musel:
1. Pochopit free-form text každého reviewera
2. Mappnout na společnou strukturu
3. Teprve pak slévat

To je víc latence i víc chyb.

## Failure modes

Co se může pokazit a jak to ošetřujeme:

| Co se pokazí                       | Chování                                        |
|------------------------------------|------------------------------------------------|
| Specialista vrátí prázdný text     | `_run_specialist` vrátí prázdný `content` — supervisor uvidí "bez nálezů" |
| Specialista timeoutne              | `asyncio.gather` čeká na všechny → padá celý běh |
| Specialista nedodrží markdown formát | Supervisor je dost robustní, aby to zvládl   |
| Supervisor vrátí nesmysl           | Uživatel vidí v terminálu i tak — můžeme ladit  |
| Síťová chyba                       | `query()` vyhodí výjimku, propadne do CLI       |

## Možné rozšíření

### Přidat 4. specialistu (např. dokumentace)

1. Napsat `DOC_PROMPT` v `prompts.py`.
2. V `orchestrator.py` přidat čtvrtý `_run_specialist()` do `asyncio.gather`.
3. Aktualizovat supervisor — ten je dost obecný, aby přidaný report zvládl.

Nic víc. Žádný refaktor.

### Loop pattern

Pokud supervisor vrátí skóre < 5, mohli bychom vzít top-3 nálezy a spustit
**druhé kolo** specialistů s instrukcí "podívej se hlouběji konkrétně tady".
To by byl klasický **Loop** pattern z hierarchie zadání.

### Conditional pattern

Před fanoutem by mohl být **preflighter** (jeden malý query):

```python
async def should_run(code: str) -> dict[str, bool]:
    """Vrátí, kteří specialisté mají smysl pro tento typ souboru."""
    ...
```

Pro `*.md` by skipnul security a performance. Pro testy možná skipnul
performance. Tím se ušetří API náklady.
