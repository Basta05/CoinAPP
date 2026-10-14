# CoinScan – Solana memecoin scanner

Desktopová appka: najde nové Solana coiny, protáhne je filtry, bezpečnostní kontrolou a AI,
uloží predikci do DB a pak 24 h sleduje cenu, aby změřila, jak úspěšné je tvoje nastavení.

## Spuštění (Windows)
1. Musíš mít nainstalovaný Python 3.10+ (se zaškrtnutým „Add to PATH“).
2. Dvojklik na **start.bat**. Poprvé vytvoří `.venv`, nainstaluje balíčky a založí `.env`.
3. V appce klikni na **🔑 .env** a doplň `OPENROUTER_API_KEY` (případně Discord).
4. **▶ Start**.

## Záložky
- **⚡ Pulse** – živý přehled pump.fun ve 3 sloupcích (New / Soon migrating / Migrated) jako na pump.fun/Axiomu.
  Klik na coin = live detail (MCap graf, bonding, RugCheck, doporučená pozice), tlačítko 🤖 AI analýza.
  Dvojklik = otevřít na pump.fun. Nahoře rychlé filtry, detailní filtry sloupců v záložce Filtry.
- **💎 Gemy** – gem detektor spočítá zdarma skóre (momentum, aktivita, bonding, ATH, komunita, bezpečnost),
  nejlepší kandidáty pošle na AI (bezpečnost + strategie vstup/SL/TP). Při BUY vyskočí popup s reportem
  a odkazy. Každý gem se 24 h sleduje – vidíš, kolik jich udělalo 2× / 5× / 10× / 20×.
- **🔄 BUY gemy** – hluboké sledování po cyklech (1 cyklus = 30 s): cena bez AI každý cyklus, Jev skóre každý
  N-tý cyklus, AI update (trend, držet / přikoupit / vybrat zisk / prodat) každý N-tý cyklus, s limity v cyklech.
  Nastavuje se nahoře v záložce 💎 Gemy. Ne-BUY gemy jdou po 5 min do archivu. Export CSV/Markdown v Úspěšnosti.
- **🏁 Runneři** – každých 10 min najde coiny, které vyletěly (MC/ATH > $300k), porovná je s tím, co appka
  viděla (tabulka `seen` = každý coin z Pulse + proč nebyl gem), a AI navrhne úpravy nastavení (tlačítko Použít).
- **AI pipeline gemů:** gem skóre (zdarma) → low = bez AI (jen tlačítkem) · watch = 🧮 Jev skóre · buy = Jev + AI report
  (free model = hlavní, placený Claude Sonnet 5 jen pojistka, když free nejde). Klíče v `.env`:
  OPENROUTER_API_KEY_FREE / OPENROUTER_API_KEY_PAID / JEV_API_KEY.
- **Scanner log / Coiny (DB) / Úspěšnost** – automatický scanner s AI a měřením úspěšnosti.
- **⚙ Filtry & presety** – všechny filtry, 4 vestavěné presety (⭐ Migration play = doporučený start)
  + ukládání vlastních presetů (složka `presets/`).
- **🛡 Risk management** – bankroll, riziko na trade, SL, TP plán, násobič podle risk ratingu.

## Jak to funguje
```
DexScreener (nové profily / boosty)
  → 1) základní filtry   (stáří, likvidita, mcap, objem, buys, socials)
  → 2) RugCheck          (mint/freeze authority, top10 holdeři, danger rizika)
  → 3) LLM přes OpenRouter (verdikt BUY / WATCH / SKIP + confidence + red flags)
  → risk rating 0–100 (LOW / MEDIUM / HIGH)
  → SQLite (data/coinscan.db) + Discord alert
Tracker každých 5 min: aktualizuje cenu → hit (≥ 2× peak) / rug / miss po 24 h
```
Ukládají se i **zamítnuté** coiny a sledují se taky – v záložce **Úspěšnost** pak vidíš, jestli
filtry opravdu vyřazují horší coiny (hit rate „passed“ vs. „rejected_*“). Na tom se dá ladit `config.yaml`.

## Soubory
| Soubor | Co dělá |
|---|---|
| `config.yaml` | všechny filtry a prahy |
| `.env` | API klíče (OpenRouter, Discord) |
| `coinscan/engine.py` | hlavní smyčka scan + tracking |
| `coinscan/dexscreener.py` | data o párech |
| `coinscan/rugcheck.py` | bezpečnostní report |
| `coinscan/llm.py` | AI kontrola (prompt je tady) |
| `coinscan/risk.py` | výpočet risk ratingu |
| `coinscan/stats.py` | vyhodnocení úspěšnosti |
| `gui/main_window.py` | okno appky |

## Poznámky
- Peak se měří jen v intervalech trackeru (default 5 min), krátké špičky mezi tím neuvidí.
- Nejde o finanční radu – jde o nástroj na testování vlastní strategie.
