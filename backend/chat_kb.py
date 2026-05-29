"""In-app chat knowledge base for common card-tax questions.

Each entry is keyed by a short slug. ``match()`` runs a lightweight keyword
score against an incoming question and returns the best entry (or None if the
score is too low to be useful).

This is deliberately not a real LLM/RAG setup — it's deterministic, fast, and
auditable, which is what a "we'll answer your boring tax questions in chat"
feature actually needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class KBEntry:
    key: str
    question: str
    answer: str
    keywords: tuple[str, ...]  # weighted; first ones matter most


# Each entry's "keywords" are the tokens we try to match against the user's
# question. Order matters — earlier keywords get a slightly larger weight
# (see _score()).
_ENTRIES: tuple[KBEntry, ...] = (
    KBEntry(
        key="taxes_on_sales",
        question="Do I need to pay taxes on card sales?",
        answer=(
            "Generally yes. If you sell a card for more than you paid (your "
            "cost basis), the gain is taxable — usually as a capital gain. "
            "Cards held over a year are long-term capital gains; under a year "
            "they're short-term and taxed as ordinary income. Cards are "
            "considered \"collectibles\" by the IRS, so long-term gains can "
            "be taxed at up to 28% (vs. the usual 15–20% for stocks). "
            "Even casual sellers owe tax on net gains."
        ),
        keywords=("taxes", "pay", "sales", "owe", "irs", "card", "selling"),
    ),
    KBEntry(
        key="1099k_threshold",
        question="What's the 1099-K threshold?",
        answer=(
            "For tax year 2025 the federal 1099-K threshold is $2,500 in "
            "gross payments (it drops to $600 in 2026). Several states set "
            "lower thresholds — MA, VT, VA, MD, IL, NJ, and DC are at "
            "$600 already. Important: even if you don't get a 1099-K, you "
            "still owe tax on net gains. The form is just paperwork; the "
            "tax liability exists regardless."
        ),
        keywords=("1099", "1099-k", "1099k", "threshold", "form", "report"),
    ),
    KBEntry(
        key="hobby_vs_dealer",
        question="What's the difference between hobby, investor, and dealer?",
        answer=(
            "Three classifications, very different tax treatment:\n\n"
            "• Hobby — you sell occasionally for fun. You report income, but "
            "you can't deduct expenses or losses (TCJA killed misc itemized).\n"
            "• Investor — you hold cards for appreciation. Gains are capital "
            "gains (collectibles rate). You can deduct losses against gains.\n"
            "• Dealer — buying and selling cards is your trade or business. "
            "Sales are ordinary income on Schedule C, but you can deduct all "
            "ordinary business expenses (and pay self-employment tax).\n\n"
            "Use the Classification quiz in CardTax for an IRS-factor-based "
            "read on which category fits you."
        ),
        keywords=("hobby", "dealer", "investor", "classification", "difference"),
    ),
    KBEntry(
        key="cost_basis",
        question="How does cost basis work?",
        answer=(
            "Cost basis is what you can subtract from the sale price before "
            "tax is calculated. It includes:\n\n"
            "• Purchase price\n"
            "• Grading fees (PSA, BGS, SGC)\n"
            "• Shipping IN (what you paid to receive the card)\n"
            "• Buyer's premiums and auction fees on the buy side\n"
            "• Break/spot price if acquired from a break\n\n"
            "Gain = (sale price − platform fees − shipping out) − basis. "
            "Keep receipts: the IRS can disallow basis you can't document."
        ),
        keywords=("cost", "basis", "deduct", "purchase", "receipt"),
    ),
    KBEntry(
        key="fifo_vs_lifo",
        question="FIFO vs LIFO — which lot method should I use?",
        answer=(
            "FIFO (first-in-first-out) is the IRS default and what most card "
            "sellers use — when you sell a card, you assume you sold the "
            "earliest acquired copy. LIFO sells the most recent first.\n\n"
            "FIFO tends to surface long-term holding periods (the older a "
            "card is, the more likely it's been held >1 year). LIFO often "
            "produces smaller gains in rising markets, but cards bought "
            "recently are usually short-term, which is taxed at higher rates. "
            "Specific identification is also allowed if you can document "
            "exactly which copy you sold (serial number, slab cert)."
        ),
        keywords=("fifo", "lifo", "lot", "method", "identification", "specific"),
    ),
    KBEntry(
        key="deduct_shipping",
        question="Can I deduct shipping costs?",
        answer=(
            "Yes, with a wrinkle:\n\n"
            "• Shipping OUT (what you paid to ship the sold card to the buyer) "
            "is a selling expense — it reduces net proceeds.\n"
            "• Shipping IN (what you paid to receive the card you bought) "
            "is part of cost basis.\n"
            "• Shipping CHARGED to the buyer is part of gross receipts (it's "
            "on your 1099-K), and the actual cost-out offsets it.\n\n"
            "CardTax breaks these out explicitly on every transaction."
        ),
        keywords=("shipping", "deduct", "postage", "ship"),
    ),
    KBEntry(
        key="gift_cards",
        question="What about cards I got as gifts?",
        answer=(
            "Gifts use a dual-basis rule:\n\n"
            "• For computing GAIN: your basis = the donor's basis (what they "
            "originally paid + their costs).\n"
            "• For computing LOSS: your basis = the LESSER of donor's basis "
            "or fair market value at the time of the gift.\n\n"
            "If you sell between the two, there's no gain and no loss. "
            "Your holding period \"tacks\" — you inherit the donor's purchase "
            "date for long-term/short-term purposes. Ask the donor for their "
            "receipt; without it, the IRS can treat basis as $0."
        ),
        keywords=("gift", "given", "donor", "donated", "received"),
    ),
    KBEntry(
        key="inherited_cards",
        question="What about cards I inherited?",
        answer=(
            "Inherited cards get a STEP-UP in basis: your cost basis is the "
            "fair market value at the date of death (or alternate valuation "
            "date six months later, if the estate elected it).\n\n"
            "All inherited property is automatically treated as long-term "
            "regardless of when you actually sell — so collectibles long-term "
            "rates apply. Get an appraisal close to the date of death; it's "
            "your documentation for basis."
        ),
        keywords=("inherit", "inherited", "death", "estate", "passed"),
    ),
    KBEntry(
        key="wash_sales",
        question="Do wash sale rules apply to collectibles?",
        answer=(
            "No. The wash sale rule (§1091) explicitly only applies to "
            "\"stock or securities.\" Trading cards are collectibles, not "
            "securities — so you can sell a card at a loss and rebuy the same "
            "card the next day and still claim the loss. (You can't do that "
            "with stocks.)\n\n"
            "This is one of the legitimate tax-loss-harvesting advantages "
            "the card market has over equities."
        ),
        keywords=("wash", "sale", "loss", "harvest", "rebuy"),
    ),
    KBEntry(
        key="collectibles_rate",
        question="What's the collectibles tax rate?",
        answer=(
            "For long-term capital gains, collectibles are taxed at a maximum "
            "of 28% (vs. 15% or 20% for most other assets). Sports cards, "
            "TCGs, and other trading cards all count as collectibles under "
            "IRC §408(m).\n\n"
            "The 28% is a CAP, not a flat rate — if your ordinary income "
            "bracket is below 28%, you pay your ordinary rate. Short-term "
            "gains (held ≤1 year) are just taxed as ordinary income."
        ),
        keywords=("collectibles", "rate", "28", "tax", "capital", "gains"),
    ),
    KBEntry(
        key="state_collectibles",
        question="What states have extra collectibles taxes?",
        answer=(
            "Most states tax capital gains as ordinary income at state rates "
            "— there isn't usually a separate \"collectibles\" rate at the "
            "state level. A few notable cases:\n\n"
            "• California taxes collectibles at full ordinary rates (up to "
            "13.3%).\n"
            "• New York adds state + NYC tax (combined up to ~14.8%).\n"
            "• 9 states have no state income tax at all (FL, TX, TN, WA, NV, "
            "WY, SD, AK, NH on wages).\n\n"
            "CardTax's tax summary includes per-state estimates. Set your "
            "state in Settings."
        ),
        keywords=("state", "states", "california", "new york", "ny", "ca"),
    ),
    KBEntry(
        key="break_spots",
        question="How do break spots work for taxes?",
        answer=(
            "Your basis in each card from a break is allocated from the spot "
            "price you paid. Two common methods:\n\n"
            "• PRO-RATA by FMV — most defensible. You allocate your spot "
            "cost across the cards you received in proportion to their fair "
            "market value at the time of the break.\n"
            "• EQUAL split — simpler but harder to defend on big hits.\n\n"
            "Cards you didn't receive (because you didn't draft them) get $0 "
            "basis. Use the Break Allocator under Tools — it does pro-rata "
            "by FMV and saves the basis line per card."
        ),
        keywords=("break", "breaks", "spot", "allocate", "pyt", "razz"),
    ),
    KBEntry(
        key="personal_use",
        question="What about cards I bought to collect, not flip?",
        answer=(
            "These are \"personal use\" assets. Tax rules:\n\n"
            "• If you sell at a GAIN, it's still a taxable capital gain at "
            "collectibles rates.\n"
            "• If you sell at a LOSS, you CANNOT deduct it. Personal-use "
            "losses are disallowed (§165(c)).\n\n"
            "The distinction between collector and investor is mostly your "
            "intent. CardTax lets you mark transactions as personal-use, "
            "which automatically zeros out losses on those sales."
        ),
        keywords=("personal", "collect", "collection", "use", "hobby"),
    ),
    KBEntry(
        key="self_employment",
        question="When do I owe self-employment tax?",
        answer=(
            "Only if you're classified as a DEALER (running a card business). "
            "Dealer income flows through Schedule C, which is subject to "
            "self-employment tax — 15.3% on the first ~$168k of net earnings "
            "(2025), then 2.9% Medicare above that.\n\n"
            "Investors and hobbyists do NOT pay SE tax — capital gains aren't "
            "subject to it. This is one reason the dealer/investor line "
            "matters so much."
        ),
        keywords=("self-employment", "self", "employment", "se", "schedule", "c"),
    ),
    KBEntry(
        key="estimated_tax",
        question="Do I need to pay quarterly estimated taxes?",
        answer=(
            "If you expect to owe $1,000+ when you file, the IRS wants "
            "quarterly estimated payments. Safe harbors avoid penalties:\n\n"
            "• Pay at least 90% of current year's tax, OR\n"
            "• Pay 100% of last year's tax (110% if AGI > $150k).\n\n"
            "Due dates: Apr 15, Jun 15, Sep 15, Jan 15. CardTax's tax summary "
            "page shows what you should send each quarter based on YTD gains."
        ),
        keywords=("quarterly", "estimated", "1040-es", "payment", "penalty"),
    ),
    KBEntry(
        key="grading_fees",
        question="Are grading fees deductible?",
        answer=(
            "Yes — grading fees (PSA, BGS, SGC, CGC, etc.) are added to your "
            "cost basis. Same goes for:\n\n"
            "• Reholder/crossover fees\n"
            "• Authentication\n"
            "• Insurance on the card\n"
            "• Vault storage if used to facilitate eventual sale\n\n"
            "Add each as a basis line in CardTax — they reduce the taxable "
            "gain when you sell."
        ),
        keywords=("grading", "psa", "bgs", "sgc", "cgc", "fees", "grade"),
    ),
    KBEntry(
        key="losses",
        question="Can I deduct losses on cards I sold below cost?",
        answer=(
            "Depends on classification:\n\n"
            "• Investor — yes. Capital losses offset capital gains. Up to "
            "$3,000 of net loss can offset ordinary income per year; excess "
            "carries forward indefinitely.\n"
            "• Dealer — yes, as ordinary business losses on Schedule C.\n"
            "• Hobby — no. Hobby losses are not deductible.\n"
            "• Personal use — no. Personal-use losses are disallowed.\n\n"
            "Documenting intent (investor vs. collector) matters."
        ),
        keywords=("loss", "losses", "deduct", "below", "sold"),
    ),
    KBEntry(
        key="like_kind",
        question="Can I do a like-kind exchange with cards?",
        answer=(
            "No — not since 2018. The Tax Cuts and Jobs Act limited §1031 "
            "like-kind exchanges to REAL PROPERTY only. Before TCJA, you "
            "could swap baseball cards and defer the gain. Today, every card "
            "trade is a taxable disposition: you owe gain on the FMV of what "
            "you traded away, calculated against your basis."
        ),
        keywords=("like-kind", "1031", "exchange", "swap", "trade"),
    ),
    KBEntry(
        key="ebay_fees",
        question="Are eBay / Whatnot platform fees deductible?",
        answer=(
            "Yes — they reduce your net proceeds. The 1099-K you get from "
            "eBay reports GROSS sales (before fees), but your actual taxable "
            "amount is sales − platform fees − payment processing − shipping "
            "out − cost basis. CardTax pulls eBay fees automatically when "
            "you connect your account; for Whatnot and others, fees come in "
            "via the CSV import."
        ),
        keywords=("ebay", "whatnot", "platform", "fees", "marketplace"),
    ),
    KBEntry(
        key="record_keeping",
        question="What records do I need to keep?",
        answer=(
            "Hold these for at least 3 years after filing (7 if you want to "
            "be safe):\n\n"
            "• Purchase receipts (eBay invoices, COMC purchases, show receipts)\n"
            "• Grading invoices\n"
            "• Shipping receipts (both directions)\n"
            "• Sale records / 1099-Ks\n"
            "• Donation appraisals (if applicable)\n"
            "• Photos of cards in case of insurance claim\n\n"
            "If you're audited and can't document basis, the IRS can treat "
            "your basis as zero — meaning the full sale price is taxable."
        ),
        keywords=("records", "documentation", "receipts", "keep", "audit"),
    ),
    KBEntry(
        key="kiddie_tax",
        question="What if my kid sells cards?",
        answer=(
            "Kiddie tax (§1(g)) applies to children under 18 (or under 24 if "
            "a full-time student supported by parents). For 2025:\n\n"
            "• First $1,350 of unearned income — tax free\n"
            "• Next $1,350 — taxed at the child's rate\n"
            "• Above $2,700 — taxed at the PARENT'S marginal rate\n\n"
            "If the child is genuinely running a business (e.g., running a "
            "Whatnot show), it might be earned income on Schedule C instead, "
            "which sidesteps kiddie tax. CardTax has a kiddie-filer toggle "
            "in Settings."
        ),
        keywords=("kid", "kids", "child", "children", "minor", "kiddie"),
    ),
    KBEntry(
        key="donation",
        question="What if I donate cards to charity?",
        answer=(
            "You can deduct the FMV at the time of donation if:\n\n"
            "• The charity is a qualified 501(c)(3)\n"
            "• You held the card more than 1 year\n"
            "• The card is used in the charity's exempt purpose (\"related "
            "use\")\n\n"
            "If the use is unrelated, your deduction is limited to your cost "
            "basis instead of FMV. Donations over $5,000 require a qualified "
            "appraisal and Form 8283. CardTax tracks donations separately so "
            "they don't show up as taxable sales."
        ),
        keywords=("donate", "donation", "charity", "501", "8283"),
    ),
    KBEntry(
        key="amt",
        question="Does AMT apply to card gains?",
        answer=(
            "Alternative Minimum Tax can apply, but the 28% collectibles "
            "rate is already at or above most AMT rates, so collectibles "
            "gains rarely create AMT liability on their own.\n\n"
            "AMT becomes more relevant if you have other AMT preference "
            "items (incentive stock options, large SALT deductions, etc.). "
            "CardTax includes an AMT estimate on the tax summary page."
        ),
        keywords=("amt", "alternative", "minimum", "tax"),
    ),
    KBEntry(
        key="nol",
        question="What's a net operating loss carryforward?",
        answer=(
            "If you're a DEALER and your Schedule C shows a net loss bigger "
            "than your other income, you may have a Net Operating Loss (NOL) "
            "that carries forward to future years.\n\n"
            "Post-2017 NOLs can only offset 80% of future taxable income "
            "and carry forward indefinitely (they no longer carry back). "
            "Investors don't get NOLs — they get capital loss carryforwards "
            "instead ($3k/yr ordinary offset, indefinite carry)."
        ),
        keywords=("nol", "operating", "carryforward", "carry-forward", "carry"),
    ),
    KBEntry(
        key="qbi",
        question="Can I take the QBI deduction?",
        answer=(
            "Only if you're a DEALER (Schedule C). The §199A Qualified "
            "Business Income deduction lets eligible self-employed people "
            "deduct up to 20% of their net business income.\n\n"
            "Phase-outs apply above ~$241k (single) / ~$483k (MFJ) for 2025. "
            "Investors don't qualify — capital gains aren't QBI. CardTax's "
            "tax summary applies QBI automatically when your classification "
            "is set to dealer."
        ),
        keywords=("qbi", "199a", "qualified", "business", "deduction"),
    ),
    KBEntry(
        key="multi_state",
        question="What if I sell in multiple states?",
        answer=(
            "Income tax is based on where you LIVE, not where the buyer is "
            "— so a buyer in California doesn't trigger CA tax for a seller "
            "in Texas.\n\n"
            "Sales tax is different — marketplaces (eBay, Whatnot, etc.) "
            "collect sales tax from buyers based on the buyer's state under "
            "marketplace facilitator laws. You generally don't have to file "
            "sales tax returns yourself for marketplace sales. Direct sales "
            "(your own store, in-person shows) can create sales-tax nexus."
        ),
        keywords=("multiple", "states", "nexus", "out-of-state"),
    ),
    KBEntry(
        key="cash_at_shows",
        question="Do I have to report cash sales at card shows?",
        answer=(
            "Yes. Cash sales are taxable income just like online sales. The "
            "absence of a 1099-K doesn't change the underlying tax liability "
            "— it just means you have to track it yourself.\n\n"
            "Best practice: keep a show log (date, location, items sold, "
            "amount, payment method). The IRS treats unreported cash "
            "aggressively if it surfaces in an audit."
        ),
        keywords=("cash", "shows", "show", "in-person", "convention"),
    ),
    KBEntry(
        key="business_expenses",
        question="What business expenses can dealers deduct?",
        answer=(
            "Dealers (Schedule C) can deduct ordinary and necessary expenses:\n\n"
            "• Supplies (sleeves, toploaders, bubble mailers, tape)\n"
            "• Shipping & postage\n"
            "• Subscriptions (130point, PriceCharting, Card Ladder)\n"
            "• Show booth fees / travel\n"
            "• Home office (if exclusively used)\n"
            "• Mileage to shows / post office\n"
            "• Bank/payment processing fees not already netted out\n"
            "• Software (including CardTax!)\n\n"
            "Investors mostly can't deduct these post-TCJA."
        ),
        keywords=("expenses", "supplies", "deduct", "business", "schedule"),
    ),
    KBEntry(
        key="how_cardtax_helps",
        question="How does CardTax help me?",
        answer=(
            "CardTax pulls your sales from eBay (and CSVs from Whatnot, COMC, "
            "etc.), tracks cost basis per card, applies the right tax "
            "treatment based on your classification (hobby/investor/dealer), "
            "and produces:\n\n"
            "• Schedule D and Form 8949 previews\n"
            "• Quarterly estimated tax recommendations\n"
            "• Tax-loss-harvesting suggestions\n"
            "• Per-state estimates\n\n"
            "It's not tax advice — talk to a CPA for that — but it removes "
            "the spreadsheet pain."
        ),
        keywords=("cardtax", "how", "help", "feature", "what does"),
    ),
    KBEntry(
        key="not_tax_advice",
        question="Is this tax advice?",
        answer=(
            "No. CardTax (and this chat) gives you general information about "
            "how card-sale taxes work, but every situation has details that "
            "matter — your state, classification, other income, prior-year "
            "history. For decisions that affect your filing, talk to a CPA "
            "or enrolled agent. We can help you organize the numbers so that "
            "conversation is shorter and cheaper."
        ),
        keywords=("advice", "cpa", "advisor", "legal", "disclaimer"),
    ),
)


_ENTRY_BY_KEY = {e.key: e for e in _ENTRIES}


def list_entries() -> list[dict]:
    """Return the public-facing entries for the chat suggestions menu."""
    return [{"key": e.key, "question": e.question} for e in _ENTRIES]


def list_entries_full() -> list[dict]:
    """Return full Q+A pairs for the standalone help/FAQ page."""
    return [
        {"key": e.key, "question": e.question, "answer": e.answer}
        for e in _ENTRIES
    ]


def get(key: str) -> Optional[KBEntry]:
    return _ENTRY_BY_KEY.get(key)


# --- Matching --------------------------------------------------------------

_STOPWORDS = frozenset(
    """
    a an and are as at be by do does for from how i if in is it its me my
    of on or that the their them they this to was were what when where which
    who why will with you your im i'm dont don't can cant can't should would
    could about into so just need any all my our we us
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9'\-]+")


def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    return [w for w in _WORD_RE.findall(text) if w not in _STOPWORDS and len(w) > 1]


def _score(question_tokens: list[str], entry: KBEntry) -> float:
    if not question_tokens:
        return 0.0
    qset = set(question_tokens)
    score = 0.0
    # Keyword matches — earlier keywords weighted more heavily.
    for i, kw in enumerate(entry.keywords):
        weight = 1.0 - (i * 0.04)
        if kw in qset:
            score += max(weight, 0.4)
        elif " " in kw:
            # Multi-word keyword: check substring against original
            if all(part in qset for part in kw.split()):
                score += max(weight, 0.4)
    # Also reward overlap with the canonical question text — catches phrasing
    # variants like "what's the threshold" matching "1099-K threshold".
    qtokens = set(_tokens(entry.question))
    overlap = len(qset & qtokens)
    score += overlap * 0.35
    return score


@dataclass(frozen=True)
class Match:
    entry: KBEntry
    confidence: float  # 0..1


def match(question: str) -> Optional[Match]:
    """Best KB entry for ``question``, or None if no good fit.

    Returns ``None`` instead of forcing a low-quality answer so the caller can
    escalate to the human queue.
    """
    qtokens = _tokens(question)
    if not qtokens:
        return None

    best: Optional[KBEntry] = None
    best_score = 0.0
    for entry in _ENTRIES:
        s = _score(qtokens, entry)
        if s > best_score:
            best_score = s
            best = entry

    if not best or best_score < 1.0:
        # Threshold tuned so a vague question like "help" returns None and
        # gets escalated, but "tax on sales?" matches taxes_on_sales.
        return None

    # Map raw score → 0..1 confidence (cap at 1.0). A score of ~3 is a strong
    # multi-keyword hit; ~1 is a single weak match.
    confidence = min(1.0, best_score / 3.0)
    return Match(entry=best, confidence=confidence)
