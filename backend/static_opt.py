"""Static asset serving with minification + cache-control.

Replaces the plain ``StaticFiles`` mount on ``/static``. Behavior:

* In production (DATABASE_URL set), CSS and JS are minified once at startup and
  served from an in-memory cache.
* Files served with a ``?v=`` query string get a long ``immutable`` cache header
  (fingerprinted assets). Other files get a short cache.
* HTML responses elsewhere in the app use ``no-cache``; this module only
  serves /static/* assets.

The minifier is intentionally tiny — comments out, collapse whitespace. Good
enough for hand-written CSS/JS in this repo; not a replacement for a real
build pipeline.
"""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import Response


# ---------------------------------------------------------------------------
# Minifiers
# ---------------------------------------------------------------------------


def _strip_block_comments(text: str) -> str:
    """Strip /* ... */ comments. Used for both CSS and JS."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def minify_css(text: str) -> str:
    text = _strip_block_comments(text)
    # Collapse whitespace around punctuation.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([{};:,>])\s*", r"\1", text)
    text = re.sub(r";}", "}", text)
    return text.strip()


def minify_js(text: str) -> str:
    # JS minification is risky — semantic whitespace, regex literals, template
    # strings. Keep it conservative: strip block comments + line comments,
    # leave whitespace alone. The wins from gzip + correctness > whitespace cuts.
    text = _strip_block_comments(text)
    # Strip // comments but not URLs (http://, https://).
    out_lines = []
    for line in text.splitlines():
        # Find // not preceded by ":" — covers http:// and https:// edge cases.
        m = re.search(r"(?<!:)//", line)
        if m:
            line = line[: m.start()].rstrip()
        out_lines.append(line)
    # Drop blank lines.
    return "\n".join(ln for ln in out_lines if ln.strip())


# ---------------------------------------------------------------------------
# Asset cache
# ---------------------------------------------------------------------------


class StaticAssets:
    """Loads files from ``directory`` and serves them with cache-control.

    ``production`` controls whether CSS/JS get minified at load time. In dev,
    every request re-reads the file so edits show up without a restart.
    """

    def __init__(self, directory: Path, *, production: bool = False):
        self.directory = directory.resolve()
        self.production = production
        # path-relative-to-directory -> (bytes, content-type)
        self._cache: dict[str, tuple[bytes, str]] = {}
        if production:
            self._warm_cache()

    def _warm_cache(self) -> None:
        for path in self.directory.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.directory).as_posix()
            body, ct = self._load(path)
            self._cache[rel] = (body, ct)

    def _load(self, path: Path) -> tuple[bytes, str]:
        ct, _ = mimetypes.guess_type(path.name)
        if not ct:
            ct = "application/octet-stream"
        data = path.read_bytes()
        if self.production:
            try:
                if path.suffix == ".css":
                    data = minify_css(data.decode("utf-8")).encode("utf-8")
                elif path.suffix == ".js":
                    data = minify_js(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
        return data, ct

    def _resolve(self, rel_path: str) -> Path:
        # Guard against ../ traversal.
        clean = rel_path.lstrip("/")
        target = (self.directory / clean).resolve()
        if not str(target).startswith(str(self.directory)):
            raise HTTPException(404)
        if not target.is_file():
            raise HTTPException(404)
        return target

    def read(self, rel_path: str) -> str:
        """Return the (possibly-minified) text contents of a file. Used by
        the critical-CSS inliner."""
        clean = rel_path.lstrip("/")
        if self.production and clean in self._cache:
            data, _ = self._cache[clean]
            return data.decode("utf-8", errors="replace")
        target = self._resolve(clean)
        body, _ = self._load(target)
        return body.decode("utf-8", errors="replace")

    def serve(self, rel_path: str, version: Optional[str] = None) -> Response:
        clean = rel_path.lstrip("/")
        if self.production and clean in self._cache:
            body, ct = self._cache[clean]
        else:
            target = self._resolve(clean)
            body, ct = self._load(target)

        # Fingerprinted asset (versioned via query string) = aggressive cache.
        # Otherwise short cache so we can ship updates.
        if version:
            cache_control = "public, max-age=31536000, immutable"
        elif clean.endswith((".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2")):
            cache_control = "public, max-age=3600, must-revalidate"
        else:
            cache_control = "no-cache"
        headers = {"Cache-Control": cache_control}
        return Response(content=body, media_type=ct, headers=headers)


# ---------------------------------------------------------------------------
# Critical CSS inlining
# ---------------------------------------------------------------------------


# Selectors we want inlined into the landing page <head>. Anything matching one
# of these prefixes is pulled out and emitted as a <style> block before the
# real stylesheets load, so the first paint isn't blocked on /static/*.css.
_CRITICAL_PREFIXES = (
    ":root",
    "html", "body",
    ".lp-topbar", ".lp-topbar-inner", ".lp-nav", ".lp-nav-right",
    ".lp-wrap", ".lp-hero", ".lp-hero-grid", ".lp-hero-eyebrow",
    ".lp-hero-sub", ".lp-hero-ctas", ".lp-hero-meta", ".lp-hero-meta-item",
    ".brand", ".btn", ".btn-primary", ".btn-secondary",
    "@font-face", "@media",
)


def _split_rules(css: str) -> list[str]:
    """Split a CSS document into top-level rules. Handles nested braces (for
    @media etc.) by counting depth."""
    rules = []
    depth = 0
    start = 0
    i = 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append(css[start : i + 1])
                start = i + 1
        i += 1
    tail = css[start:].strip()
    if tail:
        rules.append(tail)
    return rules


def extract_critical_css(*sources: str) -> str:
    """Walk one or more CSS sources, keep only top-level rules whose selector
    starts with one of the critical prefixes. Concatenate the survivors."""
    out: list[str] = []
    for src in sources:
        src = _strip_block_comments(src)
        for rule in _split_rules(src):
            head = rule.split("{", 1)[0].strip() if "{" in rule else rule.strip()
            if not head:
                continue
            if any(head.startswith(p) for p in _CRITICAL_PREFIXES):
                out.append(rule)
    return minify_css("\n".join(out))
