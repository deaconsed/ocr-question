class MarkdownPreprocessor {
  // Inline math regex — must stay in sync with CustomMarkdownWidget.
  // Handles \frac{}{}, multiple consecutive brace groups, nested braces,
  // and adjacent non-ASCII characters like Ω, α sitting right after closing $.
  static final RegExp _inlineMathRegex = RegExp(
    r'(?<!\$)\$(?!\$)((?:[^${}\\]|\\(?:[^{]|\{(?:[^{}]|\{[^{}]*\})*\}))*(?:\{(?:[^{}]|\{[^{}]*\})*\})*)+\$(?!\$)',
    multiLine: true,
  );

  /// Preprocesses the input markdown string to handle common HTML entities,
  /// tags, and LaTeX content before rendering with the markdown engine.
  String preprocessMarkdownEngine(String markdown) {
    if (markdown.isEmpty) return markdown;
    String processed = markdown;

    // ─── STEP 1: Protect LaTeX blocks from HTML processing ──────────────────
    // Temporarily replace LaTeX with placeholders so HTML cleanup doesn't
    // accidentally mangle math content like \frac{1}{2} or $x^{2}$
    final latexPlaceholders = <String, String>{};
    int placeholderIndex = 0;

    String protect(RegExp pattern, String input) {
      return input.replaceAllMapped(pattern, (match) {
        final key = '%%LATEX_${placeholderIndex++}%%';
        latexPlaceholders[key] = match.group(0)!;
        return key;
      });
    }

    // Protect in order: longest/most specific first to avoid partial matches
    // Protect display math \[ ... \]
    processed = protect(
      RegExp(r'\\\[[\s\S]*?\\\]', multiLine: true),
      processed,
    );
    // Protect display math $$ ... $$
    processed = protect(
      RegExp(r'\$\$[\s\S]*?\$\$', multiLine: true),
      processed,
    );
    // Protect inline math \( ... \)
    processed = protect(
      RegExp(r'\\\([\s\S]*?\\\)', multiLine: true),
      processed,
    );
    // Protect inline math $ ... $ — same regex as _normalizeLatexFormulas
    processed = protect(_inlineMathRegex, processed);

    // ─── STEP 2: HTML Entity Decoding ───────────────────────────────────────
    const htmlEntities = {
      '&amp;': '&',
      '&lt;': '<',
      '&gt;': '>',
      '&nbsp;': ' ',
      '&quot;': '"',
      '&apos;': "'",
      '&ndash;': '–',
      '&mdash;': '—',
      '&laquo;': '«',
      '&raquo;': '»',
      '&times;': '×',
      '&divide;': '÷',
      '&plusmn;': '±',
      '&deg;': '°',
      '&micro;': 'µ',
      '&alpha;': 'α',
      '&beta;': 'β',
      '&gamma;': 'γ',
      '&delta;': 'δ',
      '&epsilon;': 'ε',
      '&theta;': 'θ',
      '&lambda;': 'λ',
      '&mu;': 'µ',
      '&pi;': 'π',
      '&sigma;': 'σ',
      '&omega;': 'ω',
      '&infin;': '∞',
      '&radic;': '√',
      '&sum;': '∑',
      '&int;': '∫',
      '&asymp;': '≈',
      '&ne;': '≠',
      '&le;': '≤',
      '&ge;': '≥',
      '&rarr;': '→',
      '&larr;': '←',
      '&uarr;': '↑',
      '&darr;': '↓',
      '&harr;': '↔',
      '&#8203;': '', // zero-width space
      '&#160;': ' ', // non-breaking space
    };

    for (final entry in htmlEntities.entries) {
      processed = processed.replaceAll(entry.key, entry.value);
    }

    // Handle numeric HTML entities e.g. &#915; or &#x3B1;
    processed = processed.replaceAllMapped(
      RegExp(r'&#x([0-9a-fA-F]+);'),
      (m) => String.fromCharCode(int.parse(m.group(1)!, radix: 16)),
    );
    processed = processed.replaceAllMapped(
      RegExp(r'&#([0-9]+);'),
      (m) => String.fromCharCode(int.parse(m.group(1)!)),
    );

    // ─── STEP 3: HTML Tag Conversion ────────────────────────────────────────

    // <br> and <hr>
    processed = processed.replaceAll(
      RegExp(r'<br\s*/?>', caseSensitive: false),
      '\n',
    );
    processed = processed.replaceAll(
      RegExp(r'<hr\s*/?>', caseSensitive: false),
      '\n---\n',
    );

    // <sub> → unicode subscript
    processed = processed.replaceAllMapped(
      RegExp(r'<sub>(.*?)</sub>', caseSensitive: false),
      (m) => _toUnicodeSubscript(m.group(1)!.trim()),
    );

    // <sup> → unicode superscript
    processed = processed.replaceAllMapped(
      RegExp(r'<sup>(.*?)</sup>', caseSensitive: false),
      (m) => _toUnicodeSuperscript(m.group(1)!.trim()),
    );

    // <b> / <strong> → markdown bold
    processed = processed.replaceAllMapped(
      RegExp(r'<(b|strong)>(.*?)</(b|strong)>', caseSensitive: false),
      (m) => '**${m.group(2)!.trim()}**',
    );

    // <i> / <em> → markdown italic
    processed = processed.replaceAllMapped(
      RegExp(r'<(i|em)>(.*?)</(i|em)>', caseSensitive: false),
      (m) => '_${m.group(2)!.trim()}_',
    );

    // <u> → no native markdown underline, strip tags keep content
    processed = processed.replaceAllMapped(
      RegExp(r'<u>(.*?)</u>', caseSensitive: false),
      (m) => m.group(1)!,
    );

    // <s> / <strike> / <del> → markdown strikethrough
    processed = processed.replaceAllMapped(
      RegExp(r'<(s|strike|del)>(.*?)</(s|strike|del)>', caseSensitive: false),
      (m) => '~~${m.group(2)!.trim()}~~',
    );

    // <code> → markdown inline code
    processed = processed.replaceAllMapped(
      RegExp(r'<code>(.*?)</code>', caseSensitive: false),
      (m) => '`${m.group(1)!}`',
    );

    // <pre><code> → markdown code block
    processed = processed.replaceAllMapped(
      RegExp(r'<pre><code.*?>([\s\S]*?)</code></pre>', caseSensitive: false),
      (m) => '```\n${m.group(1)!.trim()}\n```',
    );

    // <p> → double newline (paragraph break)
    processed = processed.replaceAllMapped(
      RegExp(r'<p[^>]*>([\s\S]*?)</p>', caseSensitive: false),
      (m) => '\n\n${m.group(1)!.trim()}\n\n',
    );

    // <ul> / <ol> — strip tags, keep inner content
    processed = processed.replaceAll(
      RegExp(r'</?[uo]l[^>]*>', caseSensitive: false),
      '\n',
    );

    // <li> → markdown list item
    processed = processed.replaceAllMapped(
      RegExp(r'<li[^>]*>([\s\S]*?)</li>', caseSensitive: false),
      (m) => '- ${m.group(1)!.trim()}',
    );

    // Headings <h1>–<h6> → markdown headings
    processed = processed.replaceAllMapped(
      RegExp(r'<h([1-6])[^>]*>(.*?)</h[1-6]>', caseSensitive: false),
      (m) {
        final level = int.parse(m.group(1)!);
        final hashes = '#' * level;
        return '\n$hashes ${m.group(2)!.trim()}\n';
      },
    );

    // Basic table → markdown (best-effort; complex nested tables won't convert perfectly)
    processed = processed.replaceAll(
      RegExp(r'<table[^>]*>', caseSensitive: false),
      '\n',
    );
    processed = processed.replaceAll(
      RegExp(r'</table>', caseSensitive: false),
      '\n',
    );
    processed = processed.replaceAll(
      RegExp(r'<thead[^>]*>|</thead>', caseSensitive: false),
      '',
    );
    processed = processed.replaceAll(
      RegExp(r'<tbody[^>]*>|</tbody>', caseSensitive: false),
      '',
    );
    processed = processed.replaceAllMapped(
      RegExp(r'<tr[^>]*>([\s\S]*?)</tr>', caseSensitive: false),
      (m) {
        final cells = RegExp(
          r'<t[hd][^>]*>([\s\S]*?)</t[hd]>',
          caseSensitive: false,
        ).allMatches(m.group(1)!).map((c) => c.group(1)!.trim()).join(' | ');
        return '| $cells |\n';
      },
    );

    // ─── STEP 4: Strip any remaining unknown HTML tags (safe fallback) ───────
    // At this point only unrecognized/malformed tags remain — strip them
    processed = processed.replaceAll(RegExp(r'<[^>]+>'), '');

    // ─── STEP 5: Whitespace cleanup ──────────────────────────────────────────
    // Collapse 3+ consecutive newlines to 2 (preserve paragraph spacing)
    processed = processed.replaceAll(RegExp(r'\n{3,}'), '\n\n');
    // Remove trailing spaces on each line
    processed = processed.replaceAll(RegExp(r'[ \t]+$', multiLine: true), '');

    // ─── STEP 6: Restore LaTeX placeholders ─────────────────────────────────
    for (final entry in latexPlaceholders.entries) {
      processed = processed.replaceAll(entry.key, entry.value);
    }

    // ─── STEP 7: Insert space between closing LaTeX delimiter and adjacent ───
    // unicode letter — package requires whitespace after closing delimiter.
    // e.g. $\frac{25}{\pi}$Ω  →  $\frac{25}{\pi}$ Ω
    processed = processed.replaceAllMapped(
      RegExp(r'(\$|\\\)|\\\]|\$\$)(?=\p{L})', unicode: true),
      (match) => '${match.group(1)} ',
    );

    return processed;
  }

  String _toUnicodeSuperscript(String text) {
    const normal = '0123456789abcdefghijklmnopqrstuvwxyz+-=()n';
    const superscript = '⁰¹²³⁴⁵⁶⁷⁸⁹ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖqʳˢᵗᵘᵛʷˣʸᶻ⁺⁻⁼⁽⁾ⁿ';
    final result = StringBuffer();
    for (final char in text.split('')) {
      final idx = normal.indexOf(char);
      result.write(
        idx != -1 && idx < superscript.length ? superscript[idx] : char,
      );
    }
    return result.toString();
  }

  String _toUnicodeSubscript(String text) {
    const normal = '0123456789abcdefghijklmnopqrstuvwxyz+-=()';
    const subscript = '₀₁₂₃₄₅₆₇₈₉ₐbcdₑfgₕᵢⱼₖₗₘₙₒₚqᵣₛₜᵤᵥwₓyz₊₋₌₍₎';
    final result = StringBuffer();
    for (final char in text.split('')) {
      final idx = normal.indexOf(char);
      result.write(idx != -1 && idx < subscript.length ? subscript[idx] : char);
    }
    return result.toString();
  }
}
