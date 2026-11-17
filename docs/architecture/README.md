# Architecture

How the finance simulator is put together: an overview in diagrams, and the
ADRs that specify it. The diagrams are a map for a reader; the ADRs are the
specification, and where the two differ the ADR is right and the diagram
needs fixing.

## The ADRs

| ADR | Covers | Plan |
| --- | --- | --- |
| [ADR-001](ADR-001-overall-architecture.md) | Design principles, the two layers, the monthly step, priority bands | |
| [ADR-002](ADR-002-storage-schema.md) | Two-layer SQLite storage, DDL, glossary | |
| [ADR-003](ADR-003-registries-and-csv-schemas.md) | Locked vocabularies, every CSV schema, scenario inheritance | |
| [ADR-004](ADR-004-reproducibility-and-activation.md) | Provenance, determinism, activation, the background builder | |
| [ADR-005](ADR-005-lots-and-per-lot-state.md) | Lots and per-lot state | [plan](ADR-005-implementation-plan.md) |
| [ADR-006](ADR-006-ensemble-scenarios.md) | Ensembles: parameter variation, Monte Carlo runs | [plan](ADR-006-implementation-plan.md) |
| [ADR-007](ADR-007-sensitivity-analysis.md) | Sensitivity analysis over ensemble parameters | [plan](ADR-007-implementation-plan.md) |
| [ADR-008](ADR-008-correlated-parameters.md) | Correlated parameters in ensembles | [plan](ADR-008-implementation-plan.md) |
| [ADR-009](ADR-009-pooled-ensembles.md) | Pooled ensembles: one result set from several | |
| [ADR-010](ADR-010-responsive-strategies.md) | Responsive strategies: code that decides a chain's draws and placements | |
| [ADR-011](ADR-011-tax-calculators.md) | Tax calculators: built-in and household code that computes each jurisdiction's return | |

## The guides

The ADRs say what the design is; the guides explain it to a person using it.
Each diagram below links the guides that go with it.

| Guide | For |
| --- | --- |
| [User guide](../user-guide.md) | Scenarios, the glossary, how tax is handled, common tasks, reading results |
| [Chains](../guides/chains.md) | Where money is drawn from and where extra money goes |
| [Ensembles](../guides/ensembles.md) | Varying rows, correlation and factors, pools, sensitivity |
| [How the simulator draws random futures](../guides/simulation-basics.md) | Seeds, streams, distributions, copulas |
| [Historical calibration](../guides/historical-calibration.md) | What numbers to put in; the template scenarios |
| [A century of rates](../guides/century-of-rates.md), [historical information](../guides/historical-information.md) | What history says about inflation, rates and returns |
| [Importing your data](../guides/importing-data.md) | Holdings, transactions and budgets from outside files |
| [Guard rails](../guides/guard-rails.md) | Lines spending should stay inside, and reading their margins |
| [State income tax](../guides/state-income-tax.md), [tax calculators](../guides/tax-calculators.md) | Each state's rules; writing a household's own |
| [Finance research](../guides/finance-research.md) | Tear sheets and fund weights with Claude |
| [Startup stock options](../guides/startup-stock-options.md) | What they are worth and how to model them |
| [A private remote for the data repo](../guides/data-repo-remote.md) | Backing up and sharing the data repo |
| [The SQLite guide](../sqlite-guide.md) | What is in a run file, and asking questions of it |

## The diagrams

Overviews (functional blocks, data flow, input tiers), then detail that
hangs off one spine, the month: tax and strategies are explained by when the
engine calls them (3a) and what crosses the boundary (4a, 4b).

Colors mean the same thing throughout: yellow is authored input, purple is
plug-in or template code, blue is the simulator's own processing, green is
SQLite state, gray is what a person reads.

| Diagram | Shows | ADRs |
| --- | --- | --- |
| [1. Functional blocks](#1-functional-blocks) | The layers, and how the three stores relate | 001, 002 |
| [2a. Data flow](#2a-data-flow) | Files to intent to run to readers | 002, 004, 006 |
| [2b. Where the inputs come from](#2b-where-the-inputs-come-from) | Edits, imports, prices, research, calibration, the nightly roll | 003, 004 |
| [2c. Ensembles](#2c-ensembles-three-kinds-of-batch) | Monte Carlo, sensitivity and pooled batches | 006, 007, 009 |
| [3a. One month in the engine](#3a-one-month-in-the-engine) | The order of a month | 001, 003, 010 |
| [3b. One debit through a chain](#3b-one-debit-through-a-chain) | Chains, refill, strategy draws | 003, 010 |
| [4a. Income tax](#4a-income-tax-accumulate-compute-settle) | Accumulate, compute, settle | 003, 011 |
| [4b. Strategies](#4b-strategies-ask-decide-post) | Ask, decide, post | 010 |
| [4c. Household code](#4c-household-code-one-contract-for-both) | Loading, snapshot, purity | 004, 010, 011 |
| [5. Input data](#5-input-data-three-tiers) | Templates, shared base, scenario folders | 003 |
| [6a. Load order](#6a-inheritance-the-load-order) | Parents, mix-ins, templates | 003 |
| [6b. Merging](#6b-inheritance-how-files-are-overlaid) | The three overlay rules | 003 |
| [6c. Worked example](#6c-inheritance-a-worked-example) | A row merge and a whole-file override | 003 |

## 1. Functional blocks

Six layers, read top to bottom, plus the machinery that runs alongside. The one idea to take from it: everything authored is plain files in git, everything in SQLite is derived and can be rebuilt, and the only code that is not the simulator's own (tax calculators, strategies) is fenced into one box that gets a read-only view and hands back an answer.

Not drawn: the registries and schema (`registries.py`, `schema.py`), which the loader, engine and reports all share, and the side tools that share the `analysis` command but none of the simulator (the Amazon categorizer, the video builder).

Guides: [User guide](../user-guide.md) (what a household does with each layer), [setup](../SETUP.md) (the two repos and activation), [notebooks](../../notebooks/README.md).

```mermaid
flowchart TB
  subgraph UI["1 · How people drive it"]
    direction LR
    cli ~~~ edit ~~~ nb ~~~ skills ~~~ sql
    cli["<b>sim CLI</b><br/>load · run · all · check"]
    edit["<b>Guided edit commands</b><br/>scenario · account · lot · scheduled<br/>calendar · chain · strategy · tax"]
    nb["<b>Jupyter notebooks</b><br/>pickers and chart widgets"]
    skills["<b>Claude skills</b><br/>scenario-authoring<br/>analysis-results"]
    sql["<b>sim query</b><br/>read-only SQL on a run"]
  end

  subgraph FEED["2 · Getting facts in"]
    direction LR
    staging ~~~ imp ~~~ prices ~~~ hist
    staging["<b>Staged edit</b><br/>written only if every scenario<br/>built on it still loads"]
    imp["<b>Importers</b><br/>holdings · flows · budget"]
    prices["<b>Price fetch</b>"]
    hist["<b>Historical calibration</b><br/>BLS · FRED · SSA tables"]
  end

  subgraph IN["3 · Inputs: plain files in git"]
    direction LR
    data ~~~ tmpl
    data["<b>Data repo</b> (private)<br/>shared tables · scenarios/<br/>household strategy + tax code"]
    tmpl["<b>Templates</b> (ship with the code)<br/>t_ scenarios · tax-law aux tables"]
  end

  subgraph CORE["4 · Core"]
    direction LR
    loader["<b>Loader</b><br/>merge the lineage · validate<br/>dollars to cents · names to ids"]
    ens["<b>Ensemble generator</b><br/>vary · correlate<br/>sensitivity · pool"]
    engine["<b>Engine</b><br/>month by month<br/>every dollar a transfer event"]
    subgraph PLUG["Plug-in code: read-only view in, answer out"]
      direction TB
      tax["<b>Tax calculators</b><br/>federal · state · local"]
      strategies["<b>Strategies</b><br/>draw · place · spend · year end"]
    end
  end

  subgraph STORE["5 · State: derived SQLite, can always be rebuilt"]
    direction LR
    intent -->|"one run: copied in whole<br/>as a snapshot, ledger added"| run
    run -->|"ensemble: one per member,<br/>each from a varied intent"| batch
    intent[("<b>Intent DB</b><br/>what was decided")]
    run[("<b>Run DB</b><br/>intent snapshot + ledger")]
    batch[("<b>Ensemble batch</b><br/>N member Run DBs<br/>+ series store · draws DB")]
  end

  subgraph OUT["6 · Reading results"]
    direction LR
    connections ~~~ reports ~~~ compare ~~~ charts
    connections["<b>Connections</b><br/>open a scenario's intent,<br/>latest run or batch stores"]
    reports["<b>Reports</b><br/>networth · income · expenses<br/>tax · gains · explain"]
    compare["<b>Compare</b><br/>scenarios · ensembles<br/>sensitivity · guard rails"]
    charts["<b>Chart library</b><br/>one set, used by CLI and notebooks"]
  end

  subgraph OPS["Alongside every layer · keeping results trustworthy"]
    direction LR
    prov ~~~ builder ~~~ ret
    prov["<b>Provenance</b><br/>code + data version on every run"]
    builder["<b>Background builder</b><br/>rebuild what went stale<br/>nightly roll · daily prices"]
    ret["<b>Retention and locks</b>"]
  end

  UI --> FEED
  FEED --> IN
  IN --> CORE
  loader --> ens --> engine
  engine <--> PLUG
  CORE --> STORE
  STORE --> OUT
  OUT ~~~ OPS

  class cli,edit,nb,skills,sql out
  class staging,imp,prices,hist,loader,ens,engine proc
  class data,tmpl input
  class tax,strategies code
  class intent,run,batch store
  class connections,reports,compare,charts out
  class prov,builder,ret note
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 2a. Data flow

One path from files to results. An ensemble is not a different engine: the vary step writes ordinary intents, and the same engine runs each. A run file carries its own copy of the intent and the plug-in source, so it stays readable after the data repo moves on. Everything from the intent on lives under the data repo's `state/`, is derived, and is pruned by `.retention.yaml`.

Guides: [Looking at your results](../user-guide.md#5-looking-at-your-results), [the SQLite guide](../sqlite-guide.md) (what is in a run file and how to ask about it), [how the simulator draws random futures](../guides/simulation-basics.md).

```mermaid
flowchart TB
  csv["<b>Scenario CSVs</b><br/>decimal dollars, percents,<br/>names, YYYY-MM"]
  shared["<b>Shared tables</b><br/>persons · aux · prices<br/>+ code-repo templates"]
  py["<b>Household code</b><br/>strategies/*.py · taxes/*.py"]

  load["<b>sim load</b><br/>walk the lineage, root first · overlay files, later wins<br/>resolve every name or fail · dollars to integer cents<br/>copy the plug-in source in"]

  intent[("<b>Intent DB</b><br/>state/intent/NAME.sqlite3<br/>a cache: delete it any time")]

  q{"ensemble_size<br/>above 0?"}

  vary["<b>Vary</b><br/>per member: draw each vary_ row from its<br/>distribution, correlated and seeded.<br/>Member 0 is the scenario as authored"]
  members[("<b>Member intents</b><br/>ordinary intents, one per member")]

  engine["<b>Engine</b>: run_scenario(intent)<br/>pure: no clock, no random numbers,<br/>no floats for money"]

  run[("<b>Run file</b>: one file is one run, never edited afterwards<br/><br/><b>intent snapshot</b>: same row ids, plug-in source<br/><b>ledger</b>: simulation_step · lot_state · transfer_event<br/>tax_year_summary · strategy_decision<br/><b>simulation_run</b>: code version · data version<br/>scenario_tree_id · who ran it · warnings")]

  batch[("<b>Batch stores</b> (ensembles)<br/>series.sqlite3: every member's curves<br/>draws.sqlite3: every drawn value")]

  read["<b>Readers</b><br/>reports · compare · notebooks · sim query"]

  csv --> load
  shared --> load
  py --> load
  load --> intent --> q
  q -->|"no: one run"| engine
  q -->|"yes"| vary --> members -->|"N runs on a worker pool"| engine
  engine --> run
  run --> batch
  run --> read
  batch --> read

  class csv,shared input
  class py code
  class load,vary,engine proc
  class intent,members,batch,run store
  class read out
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 2b. Where the inputs come from

What writes to the files diagram 2 starts from. Everything a command writes
to a scenario goes through a staged edit; nothing but the builder's nightly
roll, daily price fetch and morning research writes without a person asking.

Guides: [Importing your data](../guides/importing-data.md), [historical calibration](../guides/historical-calibration.md) (what numbers to put in, and the template scenarios built from them), [finance research](../guides/finance-research.md), [bringing the plan up to date](../user-guide.md#421-bring-the-plan-up-to-date-each-year), [a private remote for the data repo](../guides/data-repo-remote.md).

```mermaid
flowchart LR
  people["<b>People, and Claude skills</b>"]
  exports["<b>Outside exports</b><br/>Quicken · brokerage · budget"]
  market["<b>Yahoo price history</b>"]
  public["<b>Public history</b><br/>BLS · FRED · SSA · Damodaran"]

  staged["<b>Staged edit</b><br/>written only if this scenario and<br/>every one built on it still load"]
  scenario_files[("<b>scenarios/NAME/</b>")]
  price_files[("<b>prices/</b>")]
  history[("<b>historical_data/</b><br/>in the code repo")]
  templates[("<b>templates/</b><br/>t_ scenarios · aux tables")]
  research["<b>Claude research</b><br/>fund and company tear sheets<br/>from public web pages"]
  allocation_files[("<b>allocations.csv</b> · <b>research/</b><br/>fund weights and the tear sheets")]
  roll["<b>Roll forward a month</b><br/>lots open at the last run's projection,<br/>months gone by become year-to-date rows"]

  people -->|"sim ... add / edit"| staged
  exports -->|"sim import, with the<br/>mappings saved in imports/"| staged
  staged --> scenario_files
  roll -->|"sim scenario roll,<br/>nightly by the builder"| scenario_files
  market -->|"sim prices fetch,<br/>daily by the builder"| price_files
  research -->|"sim symbol research,<br/>one symbol each morning by the builder,<br/>each about monthly"| allocation_files
  public -->|"analysis historical"| history
  history -.->|calibrates| templates

  class people,exports,market,public,research out
  class staged,roll proc
  class scenario_files,price_files,allocation_files,history,templates input
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 2c. Ensembles: three kinds of batch

One recipe, an ordinary scenario whose `vary_` rows give each uncertain
number a distribution, feeds two kinds of run. A pool runs nothing new: it
is a weighted selection of members other ensembles already ran.

Guides: The [ensembles guide](../guides/ensembles.md) explains what the diagram leaves out, with its own diagrams: [correlated parameters](../guides/ensembles.md#correlated-parameters), [factors](../guides/ensembles.md#modeling-a-shared-driver), [pooling](../guides/ensembles.md#pooling-several-kinds-of-trouble) and [sensitivity](../guides/ensembles.md#which-varying-rows-matter-and-when-sensitivity). Also [how the simulator draws random futures](../guides/simulation-basics.md) and [a century of rates](../guides/century-of-rates.md).

```mermaid
flowchart TB
  recipe[("<b>Recipe intent</b><br/>an ordinary scenario plus vary_ rows,<br/>ensemble_size and seed")]

  monte_carlo["<b>Monte Carlo</b>: sim run RECIPE<br/>N members, every varying row drawn,<br/>keyed by seed, member and row name<br/>member 0 is the scenario as authored"]
  one_at_a_time["<b>Sensitivity</b>: sim run RECIPE --sensitivity<br/>one row at a time moved to quantiles of its<br/>own distribution (p10 and p90 by default)<br/>1 + rows × quantiles members"]

  mc_batch[("<b>state/runs/RECIPE/monte-carlo/BATCH/</b><br/>member runs · draws.sqlite3 · series.sqlite3")]
  oat_batch[("<b>state/runs/RECIPE/sensitivity/BATCH/</b><br/>member runs")]

  others[("<b>Other ensembles' Monte Carlo batches</b><br/>same span, usually the same seed")]
  pool["<b>Pool</b>: sim run POOL<br/>member j of the pool is member j of one<br/>component, shared out by weight;<br/>the files are hard-linked, nothing reruns"]
  pool_batch[("<b>state/runs/POOL/monte-carlo/BATCH/</b><br/>reads as any Monte Carlo batch")]

  read["<b>Readers</b><br/>sim ensemble summary · compare · draws<br/>sim ensemble sensitivity: one-at-a-time table from a<br/>sensitivity batch, rank correlation from a Monte Carlo batch"]

  recipe --> monte_carlo --> mc_batch
  recipe --> one_at_a_time --> oat_batch
  mc_batch --> pool
  others --> pool
  pool --> pool_batch
  mc_batch --> read
  oat_batch --> read
  pool_batch --> read

  class recipe,mc_batch,oat_batch,others,pool_batch store
  class monte_carlo,one_at_a_time,pool proc
  class read out
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 3a. One month in the engine

The order things happen in a month. This is where tax and strategies attach, so the two detail diagrams below hang off steps 3 to 9. Purple steps are where household or built-in strategy code is asked.

Guides: [Calendar for more complex changes](../user-guide.md#48-calendar-for-more-complex-changes), [the glossary](../user-guide.md#2-glossary).

```mermaid
flowchart TB
  s0(["last month's lot_state"])
  c1["<b>1 · Calendar events</b> dated this month<br/>set_rate · set_amount · retire_scheduled<br/>one_off_transfer · transfer_balance · swap_aux_table"]
  c2["<b>2 · Inflation indexes</b> advance"]
  c3["<b>3 · Strategy hooks</b><br/>on_breach (January) · year_start (January) · month_start"]
  p1["<b>4 · Phase 'rules'</b>: scheduled rules, by priority<br/>100 growth and interest<br/>200 income: salary · Social Security · RMD · IRMAA<br/>300 expenses · mortgage<br/>500 conversions and one-offs<br/>900 on_death<br/><i>a strategy that owns a rule answers <b>scale</b> before it posts;<br/>every debit goes through a source chain (next diagram)</i>"]
  pc["<b>5 · Phase 'card_interest'</b><br/>interest on what the month's payments left unpaid on a card"]
  p2["<b>6 · Phase 'tax_settlement'</b> (April only)<br/>last year's return: federal, then state, then local"]
  p3["<b>7 · Phase 'fill_bracket'</b><br/>Roth conversion up to a bracket top<br/><i>5 to 7 have no priority number. They follow the rules for a stated<br/>reason: interest is on what the payments left, an RMD cannot be<br/>converted, and the conversion needs the year's whole income</i>"]
  ye["<b>8 · Strategy hook: year_end</b> (December)<br/>harvest gains or losses · gift · sell to rebalance"]
  me["<b>9 · Month end</b><br/>overflow sweep through destination chains<br/><i>a strategy that owns the chain answers <b>place</b></i><br/>basis step-up after a death"]
  s1(["this month's lot_state, one row per lot"])

  s0 --> c1 --> c2 --> c3 --> p1 --> pc --> p2 --> p3 --> ye --> me --> s1

  class c1,c2,p1,pc,p2,p3,me proc
  class c3,ye code
  class s0,s1 store
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 3b. One debit through a chain

Every debit resolves the same way, whoever asked for it. A strategy can choose the lots, but the engine posts the legs, and whatever the strategy leaves undecided falls through to the chain as written.

Guides: [Chains: where money comes from, and where extra money goes](../guides/chains.md), [chains as default policies](../user-guide.md#46-chains-as-default-policies).

```mermaid
flowchart TB
  need["<b>A debit needs $X from a chain</b><br/>a rule, a calendar transfer, an RMD, a tax payment"]
  own{"does a strategy<br/>own this chain?"}
  draw["<b>draw(ctx, chain, need)</b><br/>answers with Takes from<br/>the chain's leaf lots"]
  steps["<b>The chain's own steps</b><br/>in file order, each with<br/>its maximum or percent"]
  leaf["<b>At each leaf lot</b><br/>keep its minimum balance · look ahead and<br/>refill it from the lot's own refill chain"]
  legs[("<b>Transfer events</b><br/>taxable lot: return_of_capital + ltcg or stcg<br/>traditional: tax_deferred_out<br/>Roth: basis first, then the five-year rule")]
  short["<b>Nothing left anywhere?</b><br/>the debit posts anyway, the balance goes negative,<br/>and one warning is recorded in the run"]

  need --> own
  own -->|yes| draw
  own -->|no| steps
  draw -->|"whatever it left undecided"| steps
  draw --> leaf
  steps --> leaf
  leaf --> legs
  leaf -.-> short

  class need,steps,leaf proc
  class draw code
  class legs store
  class short note
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 4a. Income tax: accumulate, compute, settle

There is no tax accumulator rule (the `tax_accumulator` kind was retired with [#507](https://github.com/BobKerns/analysis/issues/507)). The ledger does the accumulating: each transfer already carries its tax character, and each April the engine sums the year into a read-only TaxYear and asks the calculators. The same prepared year answers every what-if, which is why `fill_bracket`, IRMAA and after-tax net worth agree with the April bill.

Guides: [How tax is handled](../user-guide.md#3-how-tax-is-handled), [state income tax, state by state](../guides/state-income-tax.md), [itemized deductions](../user-guide.md#49-itemized-deductions), [IRMAA](../user-guide.md#414-medicares-income-surcharge-irmaa).

```mermaid
flowchart TB
  ev[("<b>All year: the ledger is the accumulator</b><br/>every transfer_event carries a tax_character:<br/>ordinary · ss_benefit · ltcg · stcg · qualified_dividend<br/>tax_deferred_out · tax_free · return_of_capital ...")]
  tax_files["<b>taxes/federal.csv · state.csv · local.csv</b><br/>kind · source_chain · destination · params_json"]
  res["<b>residence.csv</b> + <b>aux tables</b><br/>the state on December 31; brackets,<br/>deductions, thresholds as of year end"]

  ty["<b>TaxYear</b>: read-only, built by the engine each April<br/>income items by character and account · spending by tag<br/>mortgages · taxes paid · persons · filing status · last year's return"]

  subgraph CALC["Calculators: pure functions, built in or the household's taxes/*.py"]
    direction LR
    fed["<b>federal</b>"]
    st["<b>state</b><br/>starts from federal AGI"]
    loc["<b>local</b><br/>starts from the state return"]
    fed -->|upstream| st -->|upstream| loc
  end

  ret["<b>Return</b> per jurisdiction<br/>agi · taxable_income · tax · carryovers · named lines"]
  sum[("<b>tax_year_summary</b><br/>federal_agi · state_tax · ...")]
  paid["<b>Already paid</b><br/>withholding and estimates: the year's<br/>transfers into the destination account"]
  settle["<b>April settlement</b><br/>tax less what was already paid: one transfer_event<br/>from source_chain to destination, or a refund to cash"]
  estimates["<b>What-if callers</b>: compute(extra income or gain)<br/>fill_bracket · after-tax net worth<br/>IRMAA's MAGI · a strategy's ctx.tax"]

  ev --> ty
  tax_files --> ty
  res --> ty
  ty -->|"prepare(year), once"| CALC
  CALC --> ret
  CALC <-.->|"the same prepared year, so no<br/>estimate can disagree with April"| estimates
  ret --> sum
  ret --> settle
  paid --> settle

  class ev,sum store
  class paid,tax_files,res input
  class ty,settle proc
  class fed,st,loc code
  class ret,estimates out
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 4b. Strategies: ask, decide, post

A strategy is a kind (code) bound to something it owns (a chain, tagged rules, or guard rails). The engine asks at fixed moments, the strategy answers with moves, and the engine posts them through its own paths. No answer means the scenario behaves as written.

Guides: User guide: [draw from whatever is overweight](../user-guide.md#416-draw-from-whatever-is-overweight-responsive-strategies), [spend less after bad years](../user-guide.md#417-spend-less-after-bad-years-spending-strategies), [tax-aware moves each December](../user-guide.md#418-tax-aware-moves-each-december-year-end-strategies), [respond when a guard rail is crossed](../user-guide.md#419-respond-when-a-guard-rail-is-crossed-strategy-triggers). Also the [guard rails guide](../guides/guard-rails.md).

```mermaid
flowchart TB
  def["<b>Scenario: strategies/NAME.csv</b> (one row)<br/>kind + params_json, and what it owns:<br/>a <b>chain</b> · the <b>rules</b> carrying a tag · guard rails as <b>trigger</b><br/>strategy_vary/NAME.csv: the params an ensemble varies"]
  code["<b>Code for the kind</b><br/>built in: rebalance · down_market · buckets · guardrail · threshold<br/>ceiling_floor · amortize · cut_on_breach · harvest · gift<br/>household: strategies/*.py, may subclass or delegate to a built-in"]

  ask["<b>The engine asks, at fixed moments</b><br/><b>draw</b>: a debit reaches an owned source chain<br/><b>place</b>: a sweep reaches an owned destination chain<br/><b>year_start</b>, then <b>scale</b> before each owned rule posts<br/><b>month_start</b> · <b>on_breach</b> (January) · <b>year_end</b> (December)"]

  ctx["<b>Context</b>: read-only, nothing after this month<br/>lots · allocation · targets (targets.csv) · budget<br/>tax estimate · history · people · life tables<br/>triggered guard rails · the strategy's own memory"]

  dec{"<b>Decision</b><br/>or None"}

  post["<b>The engine checks the moves and posts them itself</b><br/><b>Take / Put</b>: which leaf lots, how much<br/><b>Spend</b>: deliver more or less than the budget<br/><b>Transfer / Realize</b>: sell, convert, gift, harvest"]
  fall["<b>The plain scenario applies</b><br/>chain steps in file order;<br/>the rule posts its budget"]
  led[("<b>Ledger</b><br/>transfer_event · strategy_decision (note + numbers)<br/>spending_baseline (budget vs delivered) · breach_event")]

  def --> ask
  code --> ask
  ask --> ctx --> dec
  dec -->|moves| post
  dec -->|"None, or the part<br/>left undecided"| fall
  post --> led
  fall --> led

  class def input
  class code code
  class ask,post,fall proc
  class ctx,dec out
  class led store
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 4c. Household code: one contract for both

Tax calculators and strategies share the same loading, snapshot and purity rules. This is what keeps a run reproducible even though it ran code from the data repo.

Guides: [Tax calculators: your own state's rules](../guides/tax-calculators.md), a worked example of household code from tag to check.

```mermaid
flowchart TB
  src["<b>Data repo</b><br/>strategies/*.py<br/>taxes/*.py"]
  load["<b>Load</b><br/>import, check API_VERSION<br/>check params against the kind's<br/>Params (units, tags, accounts)"]
  snap[("<b>Intent DB</b><br/>strategy_source · tax_source<br/>module, sha256, source text")]
  run["<b>Run</b><br/>imports from the snapshot,<br/>never from the data repo"]
  rf[("<b>Run file</b><br/>carries the exact code it ran")]
  chk["<b>sim strategy check</b> · <b>sim tax check</b><br/>run twice in fresh processes with<br/>different hash seeds, compare every answer"]
  tree["<b>scenario_tree_id</b><br/>editing the code changes the data<br/>version, so stale runs rebuild"]

  src --> load --> snap --> run --> rf
  src -.-> tree
  snap -.-> chk

  class src code
  class load,run proc
  class snap,rf store
  class chk,tree note
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 5. Input data: three tiers

Where each kind of file lives, and which tier stands in for which. Templates ship with the code; the data repo keeps only what the household changes; a scenario folder holds one scenario's own rows.

Guides: [templates/README.md](../../templates/README.md) (every CSV column), [scenarios build on each other](../user-guide.md#1-scenarios-build-on-each-other), [template scenarios](../guides/historical-calibration.md#template-scenarios).

```mermaid
flowchart TB
  subgraph T["Tier 1 · Code repo: templates/ (read-only, versioned with the code)"]
    direction LR
    template_aux["<b>templates/aux/</b><br/>tax law and demographics:<br/>brackets · deductions · RMD factors<br/>life tables · state tables"]
    template_scenarios["<b>templates/scenarios/t_*</b><br/>inflation and market factors,<br/>long-run series, ensembles,<br/>episodes (crisis, spike)"]
  end

  subgraph S["Tier 2 · Data repo root: shared by every scenario"]
    direction LR
    who["<b>persons.csv</b><br/>securities.csv · allocations.csv"]
    shared_aux["<b>aux/</b><br/>only what the household changes,<br/>above all filing_status.csv"]
    shared_prices["<b>prices/</b> · <b>itemizations/</b>"]
    shared_code["<b>strategies/*.py</b> · <b>taxes/*.py</b><br/>household code"]
    simp["<b>imports/</b><br/>saved mappings<br/>(the loader never reads it)"]
  end

  subgraph N["Tier 3 · scenarios/NAME/: one folder per scenario"]
    direction LR
    head["<b>scenario.csv</b> (one row)<br/>start · end · seed<br/>parent_scenario · meta_json<br/>+ README.md"]
    what["<b>What exists</b><br/>accounts.csv<br/>account_lots.csv<br/>lifespan.csv · residence.csv"]
    flows["<b>What happens</b><br/>scheduled.csv: recurring rules<br/>calendar.csv: dated changes<br/>year_to_date.csv"]
    route["<b>Where money goes</b><br/>chains/source/*.csv<br/>chains/destination/*.csv"]
    policy["<b>Policy</b><br/>strategies/*.csv · targets.csv<br/>taxes/*.csv · guard_rails.csv"]
    assume["<b>Assumptions</b><br/>series/*.csv: growth and inflation<br/>optional own aux/ prices/ itemizations/"]
  end

  template_aux -->|"same file name in the<br/>data repo replaces it"| shared_aux
  template_scenarios -->|"a t_ name with no folder in the<br/>data repo is read from here"| head
  shared_aux -->|"default unless a scenario<br/>ships its own file"| assume
  shared_prices --> assume
  who --> what
  shared_code -->|"kind"| policy

  class template_aux,template_scenarios,who,shared_aux,shared_prices,simp,head,what,flows,route,policy,assume input
  class shared_code code
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

The kinds of scenario folder:

| Kind | What marks it | Runs by itself |
| --- | --- | --- |
| Ordinary | `start` and `end` somewhere in its lineage | yes |
| Mix-in only | no dates anywhere in its lineage | no: it is mixed into others |
| Ensemble recipe | merged `meta_json` has `ensemble_size` above 0; `vary_` rows | yes, as a batch of members |
| Pool | its own `meta_json` has `pool`; the folder holds `scenario.csv` only | yes, from its components' batches; nothing loads it |
| Template | a `t_` name; read from the code repo unless the data repo has the folder | yes; read-only |

What a scenario folder holds, and how each file is overlaid down the lineage
(diagram 6b):

| File | Holds | Overlaid by |
| --- | --- | --- |
| `scenario.csv` | dates, seed, `parent_scenario`, `meta_json` | field (blank inherits); `meta_json` key by key |
| `README.md` | title and description | not inherited |
| `accounts.csv` | accounts: kind, subkind, owner, growth series, chains | row: `name` |
| `account_lots.csv` | lots: symbol, opening balance and basis | row: `account_name` + `lot` |
| `scheduled.csv` | rules: income, expenses, RMD, Social Security, mortgage, IRMAA, `fill_bracket` | row: `name` |
| `calendar.csv` | dated changes, and an ensemble's `vary_` rows | row: `name`, else the row's key columns |
| `lifespan.csv`, `year_to_date.csv`, `guard_rails.csv` | ages at death, the partial first year, rails | row: `person`, `name`, `name` |
| `residence.csv` | where the household lives, by date | row: `effective_date` |
| `targets.csv` | allocation target sets | row: `target_set` + `as_of` + `asset_class` |
| `chains/source/`, `chains/destination/` | where money is drawn from and swept to | whole file, by chain name |
| `taxes/<jurisdiction>.csv` | the income taxes | whole file |
| `strategies/`, `strategy_vary/` | responsive strategies, and what an ensemble varies of them | whole file, by strategy name |
| `series/`, `aux/`, `prices/`, `itemizations/` | rate projections, and overrides of shared tables (`itemizations/` are the `@name` sums) | whole file, by file name |

## 6a. Inheritance: the load order

An example scenario, `bear_2030`, built on the prototype's `base` and `retire_alice` (`hsa_2027` and `bear_2030` are made up for the picture). Solid arrows are parents, dotted ones mix-ins. The numbers are the order the folders are overlaid in.

Guides: [Scenarios build on each other](../user-guide.md#1-scenarios-build-on-each-other).

```mermaid
flowchart TB
  e["<b>1 · t_empty</b><br/>fallback dates and seed"]
  inf["<b>2 · t_inflation_factors</b>"]
  ins["<b>3 · t_inflation_series</b>"]
  mf["<b>4 · t_market_factors</b>"]
  ms["<b>5 · t_market_series</b>"]
  b["<b>6 · base</b><br/>the household: accounts, rules,<br/>chains, taxes<br/><i>parent_scenario =<br/>t_inflation_series:t_market_series</i>"]
  ra["<b>7 · retire_alice</b><br/>only what differs from base"]
  h["<b>8 · hsa_2027</b><br/>mix-in only: no dates,<br/>cannot run alone"]
  fc["<b>9 · t_financial_crisis</b><br/>template episode: a few<br/>calendar rows"]
  me["<b>10 · bear_2030</b><br/><i>parent_scenario =<br/>retire_alice:hsa_2027:t_financial_crisis</i>"]

  e --> inf --> ins --> b
  inf --> mf --> ms --> b
  b --> ra ==>|main parent| me
  ins --> fc
  ms --> fc
  h -.->|mix-in| me
  fc -.->|mix-in| me

  rule["<b>Numbers are the load order.</b> The main parent's whole line comes first, root first.<br/>Each mix-in then adds only the part of its own line not already listed (9's parents are 3 and 5).<br/>The scenario itself is last. Files are overlaid in that order, so <b>later wins</b>."]:::note
  me --- rule

  class e,inf,ins,mf,ms,fc code
  class b,ra,h,me input
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 6b. Inheritance: how files are overlaid

Each file kind follows one of three rules as the loader walks the load order.

```mermaid
flowchart TB
  top["<b>Each file kind has one of three overlay rules</b>, applied down the load order"]:::note
  rows["<b>Row by key</b><br/>accounts (name) · account_lots (account + lot)<br/>scheduled (name) · lifespan (person)<br/>calendar (name, else its fields)<br/>year_to_date · targets · residence<br/><br/>a new key adds a row<br/>the same key replaces the whole row<br/>no delete: end-date the rule instead"]
  whole["<b>Whole file by name</b><br/>chains/ROLE/NAME.csv<br/>strategies/NAME.csv · strategy_vary/NAME.csv<br/>taxes/JURISDICTION.csv<br/>series/ · aux/ · prices/ · itemizations/<br/><br/>the nearest file wins outright; then the<br/>data repo root, then templates/aux<br/>(series/ has no shared root)"]
  fields["<b>Field by field</b><br/>scenario.csv: a blank cell inherits<br/>meta_json: merged key by key<br/>(ensemble_size, reserve, after_tax, roll ...)<br/><br/>a template's dates and seed are only<br/>fallbacks, so mixing one in<br/>cannot move the scenario's span"]
  out[("<b>One intent DB</b><br/>the merged scenario, plus parents_json and load_order_json<br/>so a run file can say what it inherited")]
  top --> rows --> out
  top --> whole --> out
  top --> fields --> out
  class rows,whole,fields proc
  class out store
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

## 6c. Inheritance: a worked example

The two commonest cases, with the prototype's personas: a child's row files,
and a whole-file kind.

```mermaid
flowchart TB
  subgraph row_files["Row files: scheduled.csv, merged by name"]
    direction LR
    parent_rows["<b>parent's rows</b><br/>Expense_Living 4000 a month<br/>Salary_Alice to 2028-12"]
    child_rows["<b>child's file</b><br/>Salary_Alice, end 2027-12<br/>Expense_Travel"]
    merged_rows[("<b>merged</b><br/>Expense_Living 4000 a month: inherited<br/>Salary_Alice to 2027-12: replaced<br/>Expense_Travel: added")]
    parent_rows --> merged_rows
    child_rows --> merged_rows
  end
  subgraph whole_files["Whole-file kinds: series/stock_us.csv"]
    direction LR
    no_file["<b>child has no file</b>"] -->|inherited| parent_wins[("the parent's file, as is")]
    own_file["<b>child has its own file</b>"] -->|replaces it whole| child_wins[("the child's file, no rows merged")]
    header_only["<b>child's file is only a header</b>"] -->|hides the parent's| nothing[("no such series")]
  end
  row_files ~~~ whole_files

  class parent_rows,child_rows,no_file,own_file,header_only input
  class merged_rows,parent_wins,child_wins,nothing store
  classDef input fill:#FFF4D6,stroke:#B8860B,color:#3b2f00
  classDef code fill:#EDE3FF,stroke:#6B46C1,color:#2a1a55
  classDef proc fill:#DCEBFF,stroke:#2B6CB0,color:#0f2a4a
  classDef store fill:#DDF5E3,stroke:#2F855A,color:#123524
  classDef out fill:#F1F3F5,stroke:#5F6B7A,color:#1f2933
  classDef note fill:#ffffff,stroke:#9aa5b1,color:#3e4c59,stroke-dasharray:4 3
```

Rules worth knowing ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.5, §4.5a, §4.1d):

- A matching row replaces every column the child's file has, blanks included;
  a column the child's header leaves out keeps the parent's value.
- There is no delete: end an inherited rule on or before the scenario's
  start, or set a year-to-date row's amount to 0.
- A mix-in loads after the main parent's whole line, so its rows win over the
  parent's. Two mix-ins that set the same thing resolve to the later one.
- A `t_` name with no folder in the data repo is read from the code repo's
  `templates/scenarios/`, so every data repo builds on the same templates
  without a copy. A data repo folder of that name replaces the template whole.

## What the diagrams leave out

- Ensembles: 2c shows the batches, not how values are drawn (distributions, correlation, latent factors, processes).
- The priority bands in 3a are the defaults; a rule can set its own priority.
- 3a leaves out the run's start (opening lot state, year-to-date catch-ups) and its tail (the last tax year is computed after the run, with no transfer posted).
- The background builder appears only as a strip in diagram 1 and two arrows in 2b; retention and locks only in diagram 1.
- The Amazon expense categorizer and the video builder, which share the `analysis` command, are not drawn.
