# Japanese tabs (日本語)

Every English tab gets a Japanese copy: 概要 (Overview), ティアシート (Tear sheet), 価格チャート (Price chart), 詳細 (Detail), 用語集 (Glossary). The reader's first language is Japanese; they have lived and worked in the US for many years and aren't familiar with investing in either country. They read the Japanese tabs, and match figures against the English ones.

Write each Japanese tab last, from the finished English tab, so the English stays the source of truth.

## Rules for every Japanese tab
- **Numbers and dates stay exactly as on the English tabs** ($400.2B, 0.20%, 4.02%, Sep 30, 1y/5y/10y). Write "Oct 27", not 10月27日. Write "~$3.5B" as 約$3.5B (drop the tilde, never both). Don't convert to 億 or 兆, and don't convert currency: the reader matches numbers between the tabs, and conversion is where errors creep in.
- **Keep tense and dating exactly:** a dated past reading must not become a present estimate.
- **Financial terms:** plain Japanese first, then the English term in parentheses on its first use *on each tab*, e.g. 経費率（expense ratio）, 純資産総額（total net assets）, 7日間SEC利回り（7-day SEC yield）, 指数（index）, 目標年（target date）. Each tab can then be read on its own, and the English term links it to the English tabs and Glossary. Don't introduce Japan-market fund vocabulary (信託報酬, 基準価額 conventions) as if familiar, and don't compare with Japanese funds.
- **Fund, issuer, index and person names** stay in their original form (Invesco QQQ Trust, Nasdaq-100).
- **Tables** keep every row, column and figure in the same order; translate the headers and text cells only. Tickers, units and dates in cells stay as they are.
- **Charts** keep the same data; translate the title and axis labels.
- **Sources** stay as they are: link titles in their original language, same URLs. Translate only the sentences around them.
- **Two similar measures keep distinct names** (7日間SEC利回り vs 分配金利回り; 基準価額ベースのリターン vs 市場価格ベースのリターン; 総経費率 vs 実質経費率), every time.
- です・ます体, short sentences.
- The checks in `overview.md` ("Simplify without drifting") apply to every Japanese tab.

## Per tab
- **概要:** a rewrite of the English Overview for the reader, not a line-by-line translation. Keep its structure (summary, five questions, each with 読み方 and 英語版での場所). Section labels: 概要, 中身は何か, コストはいくらか, これまでの成績, 値動きの大きさ, 持つ前に知っておきたいこと, 読み方, 英語版での場所. About 1,800–2,800 Japanese characters. End with: この概要は英語版タブ（Tear sheet / Detail / Glossary）に基づいています。投資助言ではありません。
- **ティアシート and 詳細:** faithful translations: the same headings in the same order, every sentence carried over, nothing added or dropped. These are what the reader checks figures against. The word budget doesn't apply to the Japanese tear sheet.
- **価格チャート:** the same chart with Japanese title and labels, and the English text translated.
- **用語集:** every English entry, in the same order as the English Glossary, so the two can be read side by side. Each heading is the Japanese term with the English term in parentheses: `## 経費率（Expense ratio）`. Translate the definition and the bullets (計算方法 / 読み方 / このファンドでは); the "このファンドでは" figure is the English "Here" figure, unchanged.

## Switching language
The first line of each Japanese tab links to its English counterpart (`English: Tear sheet`), and the first line of each English tab links to its Japanese one (`日本語: ティアシート`), as SKILL.md describes.
