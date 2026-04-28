# Bezpečnost a rozsah projektu

## Co tento repozitář **je**

Ukázka **multi-agent orchestrace** postavené na Claude Agent SDK —
odevzdávka pro kurz Vibe Coding 1. Slouží jako **přehled kódu**, ne jako
produkční nasaditelná aplikace.

## Co tento repozitář **není**

- Není to produkční nástroj — chybí auth, rate limiting, input sanitization.
- Není auditován penetračním testem.
- Neobsahuje žádné reálné API klíče, jen placeholder v `.env.example`.

## Důležité: `examples/vulnerable_login.py`

Tento soubor obsahuje **úmyslně chybový kód** — SQL injection, hardcoded
credentials, slabý hash, command injection, N+1 dotazy.

🚨 **Nikdy ho nespouštěj v produkci.** Slouží **výhradně** jako vstupní
fixture pro reviewera, aby specialisté měli co najít.

Soubor není importovaný z `reviewer/` modulu a nikdy se nespustí omylem.

## Před spuštěním zkontroluj

### 1. API klíče

Pokud nemáš nainstalované Claude Code (`claude` na PATH), SDK potřebuje
`ANTHROPIC_API_KEY`. Vytvoř `.env`:

```bash
cp .env.example .env
# vyplň reálnou hodnotu
```

`.gitignore` filtruje `.env` ze všech commitů. **Nikdy** klíč necommituj.

### 2. Web server

`python -m reviewer.web` spouští FastAPI server na `127.0.0.1:8000`.
Server **nemá autentizaci** — kdokoliv s přístupem k localhost ho může
volat. Nikdy nevystavuj na veřejnou IP bez auth vrstvy:

- ❌ `uvicorn reviewer.web:app --host 0.0.0.0 --port 8000`
- ✅ `uvicorn reviewer.web:app --host 127.0.0.1 --port 8000`

### 3. Co se posílá do API

Veškerý kód, který do reviewera vložíš, se odešle do Claude API. Pokud
reviewuješ proprietární kód, ujisti se, že je to v souladu s tvojí firemní
politikou (Anthropic má politiku, že API requesty se neukládají na trénink,
ale konkrétní rozsah si ověř sám/sama).

### 4. Kdo platí náklady

Specialisté + supervisor stojí ~$0.04 za jeden review. Při napojení na
GitHub Actions nebo CI to může rychle narůst. Sleduj `OTEL_METRICS` nebo
si do CLI přidej `max_budget_usd` v `ClaudeAgentOptions`.

## Hlášení problémů

Pokud najdeš bezpečnostní problém, otevři issue na GitHubu.
