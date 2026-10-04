# Unescaping MuseumPlus "HTML" fields — the lvl2 pass

**Status: not implemented.** This is the second of the three rewrites in the **lvl2 conversion**
(`LidoTool.to_lvl2_single` → `LinkChecker.my_unescape_html`, `zml2lido/linkChecker.py:193‑206`),
the Python/lxml pass that runs *after* the XSLT. It is the smallest of the three and, on the sample
so far, a **no-op** — but it is cheap and its absence is a silent difference from zml2lido. The
XQuery below was run against BaseX 12.4 on this box.

## What it is for

MuseumPlus stores certain free-text fields as escaped HTML — the cataloguer typed rich text and RIA
handed it back as `&lt;div&gt;…&lt;/div&gt;`. The lvl1 stylesheet copies those values verbatim into
`lido:descriptiveNoteValue`, so the LIDO a harvester sees contains literal `&lt;div&gt;` markup.
lvl2 unescapes it and flattens it to plain text.

## Exact behaviour to reproduce (`zml2lido/unescape_html.py`)

Called on each `descriptiveNoteValue` node; the node's **`.text` is replaced**, nothing else in the
element is touched:

```python
txtN.text = unescape_html(txtN.text)
```

`unescape_html(raw)`:

1. `None → None`. Else `raw.strip()`.
2. `""` → `""`.
3. **Decide if there is markup at all:**
   - contains `&lt;` → `html.unescape()` first (the value is escaped HTML);
   - else contains `<div` (already unescaped upstream) → use as-is;
   - else (plain text, e.g. `"oben links"`) → return the stripped text unchanged, no parsing.
4. Parse with `BeautifulSoup(..., "html.parser")`, then:
   - replace **every** `<br>` with `"\n\n"`;
   - `get_text(separator=" ", strip=False)`;
5. `re.sub(r"\n\n\s+", "\n\n", text)` — trim whitespace after a paragraph break.

The semantics that matter:

- **Only `<br>` becomes a line break.** `get_text(separator=" ")` joins the text of block elements
  with a *space*, so `<div>a</div><div>b</div>` → `"a b"`, not two lines. Paragraph separation comes
  entirely from `<br>` → `"\n\n"`.
- Leading/trailing whitespace is stripped; internal spacing is preserved except the `\n\n\s+` cleanup.
- Idempotence-ish: plain text passes through unchanged, so re-running is harmless for those.
- **Scope:** only `descriptiveNoteValue` (in `objectIdentificationWrap/objectDescriptionWrap/
  objectDescriptionSet`). Other HTML-ish MuseumPlus fields are *not* unescaped here — a decision,
  not an oversight. Keep it explicit.

Worked example: `&lt;div&gt;[…]Tonstein…&lt;/div&gt;&lt;br&gt;&lt;div&gt;(Text: …)&lt;/div&gt;`
→ `"[…]Tonstein… \n\n (Text: …)"` (tags gone, `<br>` → blank line, everything else single-spaced).

## Reality check on the data

In the lvl1 sample `test/group416397-chunk1.lido.xml` there are **84** `descriptiveNoteValue` nodes
and **0** contain `&lt;` or `<div` — they are all plain text (`"oben links"`). So this pass is
currently a no-op on the sample. Implement it because it is cheap and because the real dump may
contain the HTML fields the docstring describes — then **measure**: count the
`descriptiveNoteValue`s that actually contain `&lt;`/`<div` on the real data before deciding it
earns a serve-time cost per record.

## Verified BaseX port

The provider runs the stylesheet inside BaseX and never lets the record leave the store, so this
belongs in the same post-transform XQuery as the related-works fixup. BaseX has **`html:parse`** (a
real HTML5 parser) but **no `html:unescape`**, so the entity reveal is explicit and `<br>` handling
is manual.

Two XQuery specific points that cost time: a `&amp;`-prefixed literal is how you write the *text*
`&lt;` (a bare `'&lt;'` is the entity `<`); and `fn:replace`'s **replacement** string cannot contain
a `\` escape, so use `&#10;` for the newline.

```xquery
declare function local:flatten($n) {
  typeswitch ($n)
    case element(br) return "&#10;&#10;"        (: every <br> -> blank line :)
    case element()   return string-join($n/node() ! local:flatten(.), " ")
    default          return string($n)
};

declare function local:unescape($raw as xs:string?) as xs:string? {
  if (empty($raw)) then $raw
  else
    let $t := replace($raw, '^\s+|\s+$', '')
    return if ($t = '') then ''
    else if (not(contains($t, '&amp;lt;')) and not(contains($t, '<div'))) then $t
    else
      let $markup :=
        if (contains($t, '&amp;lt;'))
        then replace(replace(replace(replace(replace($t,
               '&amp;lt;', '<'), '&amp;gt;', '>'), '&amp;quot;', '"'),
               '&amp;apos;', "'"), '&amp;amp;', '&amp;')
        else $t
      let $node := html:parse($markup)
      let $text := string-join($node/html/body/node() ! local:flatten(.), " ")
      return replace($text, '\n\n\s+', '&#10;&#10;')
};
```

Run on this box, it returns:

```
escaped -> [Zeile eins Zeile zwei
            Absatz 2 & mehr]      (: divs joined by a space, <br> -> blank line :)
plain   -> [oben links]
empty   -> []
none    -> empty-sequence
```

which matches the Python for the same inputs, including the `&amp;amp;` → `&` one-pass decode
(replace `&amp;amp;` last, exactly as `html.unescape` does).

**Differences from the Python, all cosmetic-or-intended:** `html:parse` is a spec-conformant HTML5
parser while BeautifulSoup uses `html.parser`, so exotic malformed input could flatten differently;
`html:parse` wraps content in `html/body`; and the entity reveal handles the five common entities
(plus everything else `html:parse` decodes natively). If the real data carries named entities beyond
those five *inside* the escaped markup (e.g. `&amp;nbsp;`) they still decode — only the five that
*reveal tags* are handled by hand.

## Wiring into the provider

In `_xslt_payload` (`oai/mapping.py:264‑279`), apply it in the post-transform chain alongside the
related-works fixup — one `copy … modify` over the pruned tree:

```xquery
for $d in $o//lido:objectDescriptionSet/lido:descriptiveNoteValue
let $v := local:unescape(string($d))
return if (exists($v)) then replace value of node $d with $v else ()
```

(Leave a node with no text alone — `replace value of node` on an empty element is fine, but match
the Python's `None` guard.)

Do it **inside the query that already produced the record**, not as a second pass per record — the
same round-trip rule the DC format follows.

## Tests to add

- escaped markup → tags gone, `<br>` → `"\n\n"`, other text space-joined;
- plain text → unchanged;
- empty value → untouched/empty;
- a value that is plain text but contains a literal `&amp;` → not double-processed;
- shape: still valid LIDO after the rewrite.

## Open question

Is `descriptiveNoteValue` really the only field that carries escaped HTML in this deployment? If the
real dump shows escaped markup elsewhere (event text, inscriptions), that is a scope decision, not
something to guess — measure first with a count of `contains(., '&lt;')` per candidate field.
