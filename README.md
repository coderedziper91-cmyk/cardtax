# CardTax

Tax tracking and reporting platform for sports card, Pokemon, MTG, and TCG sellers.

## Quick Start

```bash
cd ~/cardtax
./start.sh
```

Or manually:

```bash
cd ~/cardtax
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload
```

Then open http://localhost:8000

## Tax Compliance

Implements the 2025 OBBBA (One Big Beautiful Bill Act) tax code for collectibles:

- 28% maximum long-term capital gains rate on collectibles (IRC §1(h)(4))
- Short-term gains taxed as ordinary income (2025 brackets)
- 3.8% Net Investment Income Tax (NIIT) for MAGI > $200k single / $250k MFJ
- 1099-K threshold: $20,000 AND 200 transactions per platform — both required (OBBBA repealed the ARPA $600 trigger, retroactive to 2022)
- Cost basis includes grading, shipping, insurance, platform fees
- No wash sale rules on collectibles (IRC §1091 only applies to stocks/securities)
- IRC §183 9-factor hobby vs. business test
- FIFO, LIFO, and specific identification lot methods

## Features

- Dashboard with tax summary and savings opportunities
- CSV import (eBay, Whatnot, generic with column mapping)
- Schedule C preview (dealers/business)
- Schedule D preview (investors)
- Hobby vs Dealer vs Investor quiz (9 IRS factors)
- Inline cost basis editing
- Dark mode

## Stack

- FastAPI + SQLAlchemy + SQLite
- Vanilla JS + Tailwind CSS (CDN)
