# Japanese tabs (日本語)

Every English tab gets a Japanese copy: 概要 (Overview), ティアシート (Tear sheet), 価格チャート (Price chart), 詳細 (Detail), 用語集 (Glossary). The reader's first language is Japanese; they have lived and worked in the US for many years and aren't familiar with investing in either country. They read the Japanese tabs, and match figures against the English ones.

Write each Japanese tab last, from the finished English tab, so the English stays the source of truth.

## Rules for every Japanese tab
- **Numbers and dates stay exactly as on the English tabs: $38.0B, 18%, 48×, Nov 19, Apr–Jun.** Write "~$3.5B" as 約$3.5B (drop the tilde, never both). Don't convert to 億 or 兆, and don't convert currency: the reader matches numbers between the tabs, and conversion is where errors creep in.
- **Keep tense and dating exactly:** a dated past reading (…と見ていました, Sep 10 時点) must not become a present estimate (…と見ています).
- **Financial terms:** plain Japanese wording first, then the English term in parentheses on its first use *on each tab*, e.g. 時価総額（market cap）, 株価収益率（P/E）, 売上高（revenue）. Each tab can then be read on its own, and the English term links it to the English tabs and Glossary. Don't introduce Japanese-market shorthand (PER, PBR, 決算短信 conventions) as if it were familiar, and don't compare with how things work in Japan.
- **Company, product and person names** stay in their original form (Take-Two, Grand Theft Auto VI). Add katakana only if it genuinely helps.
- **Tables** keep every row, column and figure in the same order; translate the headers and text cells only. Tickers, units and dates in cells stay as they are.
- **Charts** keep the same data; translate the title and axis labels.
- **Sources** stay as they are: link titles in their original language, same URLs. Translate only the sentences around them.
- **Two similar measures keep distinct names** (ネットブッキングの会社予想 vs 売上高の会社予想), every time.
- Polite plain style (です・ます), short sentences.
- The checks in `overview.md` ("Simplify without drifting") apply to every Japanese tab.

## Per tab
- **概要:** a rewrite of the English Overview for the reader, not a line-by-line translation. Keep its structure (summary + five questions, each with 読み方 and 英語版での場所). Section labels: 概要, 何をしている会社か, 成長しているか, 利益は出ているか, 企業価値と割高・割安, 今後の注目点とリスク, 読み方, 英語版での場所. For private companies, replace the middle three with: どのくらいの規模か・その根拠は, 投資家はいくらの価値をつけたか, 上場企業にとってなぜ重要か. About 2,000–3,000 Japanese characters. End with one line: この概要は英語版タブ（Tear sheet / Detail / Glossary）に基づいています。投資助言ではありません。
- **ティアシート and 詳細:** faithful translations: the same headings in the same order, every sentence carried over, nothing added or dropped. These are what the reader checks figures against. The word budget doesn't apply to the Japanese tear sheet.
- **価格チャート:** the same chart with Japanese title and labels, and the English text translated. A private company has no price tab.
- **用語集:** every English entry, in the same order as the English Glossary, so the two can be read side by side. Each heading is the Japanese term with the English term in parentheses: `## 株価収益率（P/E）`. Translate the definition and the bullets (計算方法 / 読み方 / この会社では); the "この会社では" figure is the English "Here" figure, unchanged.

## Switching language
The first line of each Japanese tab links to its English counterpart (`English: Tear sheet`), and the first line of each English tab links to its Japanese one (`日本語: ティアシート`), as SKILL.md describes.
