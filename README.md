# Notion → Todoist sync

Jednoduchý Python script, ktorý automaticky kopíruje nové úlohy z Notion databázy do Todoistu. Pre tých, čo plánujú v Notione, ale denne taskujú v Todoiste.

Postavené cez [Claude Code](https://claude.com/claude-code) ako súčasť môjho [portfolia](https://github.com/veronkabathory-ops).

## Čo to robí

- Načíta úlohy z konkrétnej Notion databázy, kde je zadaný používateľ priradený ako "Responsible" a status nie je `Done` ani `Archived`
- Pre každú novú úlohu vytvorí task v Todoiste (do zvoleného projektu, s vlastnými labelmi)
- Pamätá si už zosynchronizované úlohy v `~/.notion_to_todoist_synced.json`, aby nevytvárala duplikáty
- Loguje do `~/.notion_to_todoist_sync.log`

Funguje len so štandardnou knižnicou Pythonu — žiadne `pip install`.

## Setup

1. **Naklonuj repo:**
   ```bash
   git clone https://github.com/veronkabathory-ops/notion-todoist-sync.git
   cd notion-todoist-sync
   ```

2. **Vytvor si `.env` súbor** podľa šablóny:
   ```bash
   cp .env.example .env
   ```
   A vyplň reálne hodnoty (viď komentáre v `.env.example`).

3. **Spusti script:**
   ```bash
   set -a; source .env; set +a
   python3 notion_to_todoist_sync.py
   ```

## Spustenie cez cron (každých 5 minút)

```cron
*/5 * * * * cd /cesta/k/notion-todoist-sync && set -a && . ./.env && set +a && /usr/bin/python3 notion_to_todoist_sync.py >> ~/.notion_to_todoist_sync.log 2>&1
```

## Bezpečnosť

- `.env` je v `.gitignore` — nikdy ho necommituj
- API tokeny si môžeš kedykoľvek revoknúť v nastaveniach Notion / Todoist
- Script používa len read-only prístup k Notion (cez tvoju integration) a write prístup do Todoist Inboxu

## Licencia

MIT
