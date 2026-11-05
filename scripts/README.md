## Pipeline Steps

### Step 1 — Filter Taxonomy (`00_filter_taxonomy.py`)

**Purpose:** Split the input specimen metadata into two pools: those identified to species level, and those identified only to genus or higher rank. Determines the `scientificName` to be used downstream for each record by traversing the taxonomic hierarchy from most to least specific.

**Run:**
```bash
python 00_filter_taxonomy.py \
    --input /path/to/specimen_metadata.csv \
    --output ./taxid_request/00_filter_taxonomy_out.csv \
    --id-col "Sample ID"
```

| Argument | Description |
|---|---|
| `--input` / `-i` | Path to input CSV/TSV |
| `--output` / `-o` | Base path for output files (script generates `_species` and `_non_species` variants automatically) |
| `--id-col` | Name of the column to use as the specimen ID (will be renamed to `ID` in output) |

**Logic:**
For each row, the script determines a `scientificName` by checking columns in this order: `species → genus → family → order → class → phylum`. The first non-empty, non-placeholder value found becomes the `scientificName`, and `identified_rank` is set accordingly. Values of `"unspecified"` or `"not collected"` are treated as empty.

Rows are then split:
- **Species output:** rows where the `species` column is populated
- **Non-species output:** rows where `species` is empty (identified to genus or higher)

**Outputs:**
| File | Description |
|---|---|
| `*_species.csv` | Records identified to species level |
| `*_non_species.csv` | Records identified to genus level or higher |
| `non_species_list.txt` | Formatted list of non-species names |

**Example output columns:** `ID`, `scientificName`, `identified_rank`, `phylum`, `class`, `order`, `family`, `genus`, `species`, `NCBI_taxid`, `NCBI_matched_rank`, `NCBI_lineage`, `lineage_mismatch`, `type_status`, `note`
- `scientificName` — the most specific available taxonomic name
- `identified_rank` — the rank at which that name was assigned (species, genus, family, etc.)
- `note` — warnings if no taxonomy could be assigned



---



### Step 2 — Non-Species Taxid Request (`requester_non-species.py`)

**Purpose:** Handles the records identified only to genus level or higher (from step 1). These cannot be submitted with a species-level taxid, and ENA requires a specific format for requesting IDs for such specimens (formatted as `[rank] sp. [Sample ID]`).

**Run:**
```bash
python requester_non-species.py \
    --input ./taxid_request/00_filter_taxonomy_out_non_species.csv \
    --output ./taxid_request/non-species_request/non-species_request_form.tsv \
    --project_id "YOUR_PROJECT_ID"
```

| Argument | Description |
|---|---|
| `--input` | Non-species CSV from Step 1 |
| `--output` | Output path for the TSV request form |
| `--project_id` | Your project identifier string (e.g. `"UKBOL_accelerated"`) |

**Logic:**
The script takes the lowest identified rank for the samples not identified to species-level, and queries the GBIF backbone taxonomy database to retrieve the GBIF usageKey for that taxonomic rank. A GBIF species URL, containing the returned usageKey is used to populate the 'description' column of the output request TSV (https://www.gbif.org/species/[usageKey]).

Where a name cannot be matched directly, the script attempts disambiguation using the available higher taxonomy to help 'anchor' a second GBIF search. Records flagged as "disambiguated" in the log are ones where the initial match was too broad and a more specific match was found using genus/family constraints.

**Outputs:**
| File | Description |
|---|---|
| `non-species_request_form.tsv` | TSV request form ready for submission |

**Diagnostics to check:**
- `Successful matches` should equal `Total samples processed`
- `Failed matches: 0` — any failures require manual resolution before proceeding
- `Disambiguated samples` — review these to confirm the correct taxid was selected

**The non-species TSV produced here is a completed request form.** Set it aside; it will be combined with species-level request forms in Step 11.



---



### Step 3 — ENA Taxid Check, Round 1 (`01_ena_taxid_check.py`)

**Purpose:** Query the ENA taxonomy API for each species-level record from Step 1. Identifies which names are already present in ENA (and taxonomically consistent), and which require further processing.

**Run:**
```bash
python 01_ena_taxid_check.py \
    --input ./taxid_request/00_filter_taxonomy_out_species.csv \
    --output ./taxid_request/01_ena_taxid_check/01_ena_check_species.csv
```

| Argument | Description |
|---|---|
| `--input` / `--in` | Species-level CSV from Step 1 |
| `--output` / `--out` | Base path for output files (script generates split files automatically) |

**Logic:**
For each record, the script:
1. Reads `scientificName` and strips any ambiguity qualifiers (e.g. 'cf.' or 'nr.') before querying (e.g. `"Genus cf. species"` → `"Genus species"`)
2. Queries the ENA suggest-for-submission API endpoint
3. Checks returned results for an exact match against `scientificName` (case-insensitive), and also checks the `otherNames` field for synonym matches
4. For each match found, fetches the full ENA lineage via the taxid endpoint, and verifies that the input `phylum` appears in the returned lineage (another homonym detection step)
5. Assigns an ENA status code to each record

| Status code | Meaning | Action |
|---|---|---|
| `MATCH_TAXONOMICALLY_CONSISTENT` | Exact match found and phylum verified | ✅ No taxid needed — excluded from further processing |
| `NO_EXACT_MATCH` | ENA returned results but none matched exactly | ⚠️ Requires GBIF search (Step 4) |
| `NO_RESULTS_RETURNED` | ENA returned no suggestions at all | ⚠️ Requires GBIF search (Step 4) |
| `MATCH_PHYLUM_UNCHECKED` | Match found but no phylum to verify against | ⚠️ Possible homonym — manual review |
| `TAXONOMIC_MISMATCH_HOMONYM` | Match found but phylum is wrong kingdom | ⚠️ Homonym — requires GBIF search |
| `MAN_VER_NAME_EMPTY` | No `scientificName` provided | ❌ Data error — fix input |
| `API_ERROR: *` | API communication failure | ❌ Retry or check connectivity |

**Outputs:**
| File | Description |
|---|---|
| `*-ena_matches.csv` | `MATCH_TAXONOMICALLY_CONSISTENT` — End |
| `*-no_ena_matches.csv` | `NO_EXACT_MATCH` / `NO_RESULTS_RETURNED` — proceed to GBIF search |
| `*-possible_homonym.csv` | `MATCH_PHYLUM_UNCHECKED` / `TAXONOMIC_MISMATCH_HOMONYM` — review |
| `*-errors.csv` | `MAN_VER_NAME_EMPTY` / `API_ERROR` — review and fix input |
| `*-filtered.csv` | All records **except** `MATCH_TAXONOMICALLY_CONSISTENT` — this is the input to Step 4 |
| `01_ena_taxid_check.log` | Full API call log with match decisions |

**The `-filtered.csv` file is the input for the next step** — it contains everything that still needs resolution.
The `*-ena_matches.csv` records have confirmed ENA taxids. These do not require a new taxid request and can be used directly in submission metadata.

**Diagnostics to check:**
- `errors.csv` should be empty — Fix sample data in input file. If any API errors suggest connectivity issues; re-run affected records
- `possible_homonym.csv` should be empty if `phylum` is consistently populated in your input data



---



### Step 4 — GBIF Backbone Search, Round 1 (`02_gbif_backbone_search.py`)

**Purpose:** For records that did not match in ENA (the `-filtered.csv` from Step 3), query the GBIF backbone taxonomy to retrieve canonical name information, synonymy, and authoritative species keys. GBIF is intended to serve as a source of truth for names not yet registered in ENA.

**Run:**
```bash
python 02_gbif_backbone_search.py \
    --input ./taxid_request/01_ena_taxid_check/01_ena_check_species-filtered.csv \
    --output ./taxid_request/02_gbif_search/02_gbif_output_1.csv
```

| Argument | Description |
|---|---|
| `-i` / `--input` | Filtered CSV from Step 3 |
| `-o` / `--output` | Output CSV path |

**Logic:**
The script implements a multi-stage search strategy for each record:
1. **Standard search.** Queries `name_backbone()` with the `scientificName` only (no taxonomic constraints). If the result is valid (has a usageKey, is not too broad), this result is used.
2. **Disambiguation.** If the stadnard search returns `usageKey=1` (Animalia root, meaning the name matched the entire animal kingdom) or a rank of `KINGDOM`/`PHYLUM`, the search is retried with `phylum`, `order`, and/or `family` as constraints. This handles cases like genus names that are homonyms across multiple families.
3. **Homonym resolution.** If the ENA status for this record was `TAXONOMIC_MISMATCH_HOMONYM`, the search goes directly to a phylum-constrained lookup, bypassing the standard search.
4. **Fuzzy lookup.** If all above stages fail to find a valid match, queries with `verbose=True` to retrieve alternative matches, filtered by phylum (and optionally family) to catch misspellings. This catches genuine misspellings in the input data (e.g. `"Etone flava"` → `"Eteone flava"`).


**Key output columns added:**
| Column | Description |
|---|---|
| `gbif_usageKey` | GBIF usage key for the matched taxon |
| `gbif_scientificName` | Full scientific name as recorded in GBIF |
| `gbif_canonicalName` | Name without authorship |
| `gbif_rank` | Taxonomic rank of the match |
| `gbif_status` | `ACCEPTED`, `SYNONYM`, `DOUBTFUL`, etc. |
| `gbif_matchType` | `EXACT`, `FUZZY`, `HIGHERRANK`, `NONE` |
| `gbif_confidence` | Match confidence score (0–100) |
| `gbif_species` | Accepted species name (useful when input is a synonym) |
| `gbif_speciesKey` | GBIF key for the accepted species |
| `gbif_notes` | How the match was resolved (e.g. `homonym_resolved_with_phylum`, `suggested_match_from_fuzzy_alternative`) |

**GBIF match types explained:**
| `gbif_matchType` | Meaning |
|---|---|
| `EXACT` | Name matched exactly |
| `FUZZY` | Name matched with spelling correction |
| `FUZZY_ALTERNATIVE` | Match came from verbose alternatives list |
| `NONE` | No match found |

**`gbif_notes` values explained:**
| Note | Meaning |
|---|---|
| *(empty)* | Standard match, no special handling |
| `homonym_resolved_with_phylum` | Phylum constraint used to resolve kingdom-level homonym |
| `disambiguated_with_phylum=X+...` | Broader match narrowed using higher taxonomy |
| `suggested_match_from_fuzzy_alternative` | Match found via verbose alternatives — check recommended |
| `no_match_found` | No match found at any stage |
| `disambiguation_failed` | Disambiguation attempted but still no valid result |
| `fuzzy_search_failed` | Fuzzy lookup also failed |
| `api_error` | GBIF API error |

**Output columns added (all prefixed `gbif_`):**
`usageKey`, `scientificName`, `canonicalName`, `rank`, `status`, `matchType`, `confidence`, `kingdom`, `phylum`, `class`, `order`, `family`, `genus`, `species`, plus key columns for each rank, `acceptedUsageKey`, and `gbif_notes`.

**Diagnostics to check:**
- A `match rate` of 100% is expected for well-curated input data — any `no_match_found` records likely have spelling errors or use names not yet in GBIF
- Records with `suggested_match_from_fuzzy_alternative` in `gbif_notes` should be checked manually — the fuzzy match may or may not be the intended taxon
- `gbif_status == 'SYNONYM'` records will have an `acceptedUsageKey` pointing to the accepted name — the GBIF processor in Step 5 handles these automatically



---


### Step 5 - Processing Name Processing (`03_gbif_name_processor.py`)

**Purpose:** This step applies a rules-based decision matrix to the GBIF backbone taxonomy match results from Step 4, determining whether the original submitted name or the GBIF-matched name should be used for each specimen in the downstream ENA taxonomy submission.

**Run:**
```bash
python 03_gbif_name_processor.py -i ./taxid_request/02_gbif_search/02_gbif_output_1.csv -r gbif_rules.csv -o ./output --project-id [PROJECT_ID[
```

| Argument | Description |
|---|---|
| `-i / --input` | Input CSV file (GBIF match results from Step 4) |
| `-r / --rules` | Rules matrix CSV file |
| `-o / --output-dir` | Output directory (default: same directory as input) |
| `-p / --project-id` | Project ID to populate in taxonomy request TSV (default: `BGE`) |

**Logic:** Each specimen's GBIF match is evaluated against a configurable rules matrix (`gbif_rules.csv`). The script compares the original scientific name against GBIF's returned match across three axes — species epithet similarity, genus similarity, and type specimen status, then combines these with the GBIF match `status` (e.g. `ACCEPTED`, `SYNONYM`, `DOUBTFUL`) and `matchType` (e.g. `EXACT`, `FUZZY`) to look up the appropriate action in the rules matrix.

For each specimen the script:
1. Extracts the species epithet and genus from both the original `scientificName` and the GBIF-returned `gbif_species` and `gbif_genus` fields.
2. Compares them to determine whether they are `same` or `different` at the epithet and genus levels.
3. Checks whether the specimen has a type designation (`type_status` column).
4. Constructs a rule key `(status, matchType, species_match, genus_match, is_type)` and looks it up in the loaded rules matrix.
5. Using the rules matrix, the script then evaluates each record's GBIF result and assigns one of the following `name_to_use` values:

| `name_to_use` | Meaning |
|---|---|
| `original` | The original `scientificName` is correct and accepted in GBIF. Proceed to taxid request. |
| `gbif (check ENA)` | GBIF found a different accepted name (e.g. the input name is a synonym). The GBIF-accepted name should be re-checked against ENA. |
| `uncertain - check placement` | GBIF match is ambiguous or taxonomic placement is uncertain. Requires manual review. |
| `not a possible combination` | GBIF flagged the name as taxonomically invalid. Requires manual review. |

**Outputs:**
| File | Description |
|---|---|
| `03_gbif_processor_request_taxid.tsv` | Ready-to-submit taxid request TSV where `name_to_use == original` records and no manual verification is required. Deduplicated by specimen name. Description column contains a GBIF species URL (`gbif_speciesKey`). Type specimens have `\| TYPE` appended to the description. `name_type` is always `published_name` (genus-level and higher requests are handled in Step 2). Any `original` record without a `gbif_speciesKey` is not written here; it is flagged for manual verification instead |
| `03_gbif_processor_check_ENA.xlsx` | Specimens where `name_to_use == gbif`, requiring ENA validation using the GBIF-matched name. Contains all input columns |
| `03_gbif_processor_annotated.xlsx` | Full input for all records, with four appended decision columns: `name_to_use`, `description_text`, `check_ENA_with_GBIF`, `manual_verification_needed` |
| `03_gbif_processor_manually_verify.xlsx` | All specimens flagged for manual verification, **plus** all GBIF spelling corrections (`name_to_use == gbif`, `check_ENA_with_GBIF == yes`), so every species still needing an ENA check is in this one file (see Step 6). Contains all input columns plus decision columns |

**Higher taxonomy checks:** The rules matrix only compares name spellings, so the input higher taxonomy is also compared against the GBIF match (only where both values are present, case-insensitive):
- **All records:** if the input `phylum` differs from `gbif_phylum`, `manual_verification_needed` is set to `yes`. This catches cross-phylum homonyms that ENA could not, because ENA only checks phylum when it already holds an exact name match (e.g. a bryozoan *Buchneria sicula* matched by GBIF to a millipede).
- **GBIF spelling corrections:** if `phylum`, `class`, `order` **or** `family` differs, `manual_verification_needed` is set to `yes` (e.g. *Phalacronotus putoni*, supplied with catfish taxonomy, corrected by GBIF to the scarab beetle *Phalacronothus putoni*). Spelling corrections without a mismatch keep `manual_verification_needed` blank but still appear in `_manually_verify.xlsx` for their `confirmed_taxonomy` to be filled in.

Each mismatch is written to the log as `→ PHYLUM MISMATCH: input '...' | GBIF '...'` (or CLASS/ORDER/FAMILY).

**Example Log Output (per sample):**
```
================================================================================
Sample: BSUIO096-24
  Scientific name (original): Teratocoris discolor
  GBIF species: Teratocoris discolor
  GBIF genus: Teratocoris
  Status: ACCEPTED
  MatchType: EXACT
  Original genus: Teratocoris | GBIF genus: Teratocoris → Genus match: SAME
  Original epithet: discolor | GBIF epithet: discolor → Species match: SAME
  Type specimen: NO
  Rule key: (ACCEPTED, EXACT, same, same, NO)
  Rule source: RULES_MATRIX
  → name_to_use: original
```

**Records assigned `original` (with confirmed GBIF matches) are formatted directly into a taxid request TSV. The `*_request_taxid.tsv` produced here is a completed request form** for programmatically resolved species. Set it aside for Step 11.

> The original `gbif_name_processor.py`, with more detailed information regarding script usage, can be found [here](https://github.com/bge-barcoding/bold-data-fun/tree/main/scripts/gbif-processor).



---



### Step 6 — Manual Verification

**Purpose:** There is a chance some specimen taxonomy cannot be resolved programatically and requires manual taxonomic review to determine the correct, currently accepted scientific name before they can be submitted to ENA. These are written to `03_gbif_processor_annotated.xlsx` and `03_gbif_processor_manually_verify.xlsx` by the GBIF Name Processor (Step 5) and should be reviewed by a taxonomist or the submitting researcher before proceeding.

For each record in `03_gbif_processor_manually_verify.xlsx`, decide on the accepted taxonomy, whether this is the original 'scientificName', 'gbif_species' name, or another name entirely. This includes the GBIF spelling-correction records (`name_to_use == gbif` with `manual_verification_needed` blank), which usually only need the corrected `gbif_species` name confirming. First append a single `confirmed_taxonomy` and a single `notes` column heading to the end of the file (duplicate headings are renamed by pandas, e.g. `confirmed_taxonomy.1`, and will not be found by Steps 7–10), and then:
- If the `scientificName` is valid and up to date, and GBIF is incorrect — copy the scientificName to the `confirmed_taxonomy` column (unchanged), and enter some supporting information (see below for guidance) in the 'notes' column.
- If the `gbif_species` name is valid and up to date - copy the gbif_species name to the `confirmed_taxonomy` column (unchanged). Not supporting information is required in the 'notes' column as we have the gbif_speciesKey to reinforce our choice.
- If neither the scientificName nor gbif_species names are correct (e.g. due to a misspelling of the scientificName) — provide the correct name in `confirmed_taxonomy` along with supporting evidence in the 'notes' column.

> Supporting evidence in 'notes' could includes a link to a taxonomic database other than GBIF, or a literature reference. Useful resources for verification include [Catalogue of Life](https://www.catalogueoflife.org/), [ITIS](https://www.itis.gov/), [WoRMS](https://www.marinespecies.org/), [Index Fungorum](https://www.indexfungorum.org/), and relevant taxonomic literature.

Only one taxid is needed per unique species, so the verified sheet may be deduplicated to one record per species (unlike non-species requests, which use per-specimen placeholder names). The four decision columns from Step 5 may be kept or removed: Step 7 works either way.

Save the completed file as `03_gbif_processor_manually_verify-complete.xlsx` before proceeding.



---



### Step 7 — ENA Taxid Check, Round 2: Post-verification (`04_post_ver_ena_check.py`)

**Purpose:** This step validates manually determined taxonomic (confirmed_taxonomy) names from Step 6 against the ENA taxonomy database, using the ENA `suggest-for-submission` API similarly to Step 3, and retrieves corresponding taxids if available.

**Run:**
```bash
python 04_post_ver_ena_check.py \
    -i ./taxid_request/03_gbif_processor/03_gbif_processor_manually_verify-complete.xlsx \
    -o ./taxid_request/04_post_ver_ena_check/04_ena_post_ver.csv \
    --man_verify_col "confirmed_taxonomy"
```

| Argument | Description |
|---|---|
| `-i` / `--input` | Path to the completed manual verification Excel file from Step 6 |
| `-o` / `--output` | Base path for output CSV files |
| `--man_verify_col` | Name of the column containing verified/corrected names (default: `confirmed_taxonomy`) |

**Logic:**
Applies the same ENA API search logic as Step 3 but reads the corrected/confirmed name from `--man_verify_col` (default `confirmed_taxonomy`) rather than `scientificName`. This ensures that any name corrections made during manual verification are checked against ENA before proceeding further — a proportion of corrected names will already exist in ENA.

Row selection depends on whether the Step 5 decision columns (`name_to_use`, `check_ENA_with_GBIF`, `manual_verification_needed`) are present:
- **Not present** (e.g. a reviewed sheet with the decision columns removed): every record is searched using `--man_verify_col`.
- **Present:** records with `manual_verification_needed == yes` are searched using `--man_verify_col`; GBIF spelling corrections (`name_to_use == gbif`, `check_ENA_with_GBIF == yes`) use `--man_verify_col` if filled in, otherwise `gbif_species`. All other records are excluded.

For each sample, the script:
1. **Gets the search term** from the specified column, stripping any ambiguous qualifiers (e.g. `Genus cf. species` → `Genus species`).
2. **Queries the ENA API** at `https://www.ebi.ac.uk/ena/taxonomy/rest/suggest-for-submission/{name}`.
3. **Finds exact matches** by comparing the search term case-insensitively against both the `scientificName` field and the `otherNames` synonyms field of each returned result.
4. **Verifies taxonomic consistency** by fetching the full lineage for each exact match via `https://www.ebi.ac.uk/ena/taxonomy/rest/tax-id/{taxid}`. If a `phylum` column is present in the input, the script checks whether the input phylum appears in the ENA lineage — mismatches are flagged as homonyms.
5. **Selects the best match**, prioritising phylum-verified matches over unverified ones.


**Outputs:** 
Same split structure as Step 3 (`-ena_matches.csv`, `-no_ena_matches.csv`, `-filtered.csv`, etc.).
| File | Contents |
|---|---|
| `results.csv` | All records with ENA columns appended |
| `results-ena_matches.csv` | `MATCH_TAXONOMICALLY_CONSISTENT` — match found in ENA |
| `results-errors.csv` | `MAN_VER_NAME_EMPTY`, `API_ERROR:*` — could not be processed. Manual check required |
| `results-possible_homonym.csv` | `MATCH_PHYLUM_UNCHECKED`, `TAXONOMIC_MISMATCH_HOMONYM` — require manual check |
| `results-no_ena_matches.csv` | `NO_EXACT_MATCH`, `NO_RESULTS_RETURNED`, `TAXONOMIC_MISMATCH_HOMONYM` — may indicate naming issues, or taxa not yet registered in ENA. Homonym records are written to **both** this file and `-possible_homonym.csv`: ENA only holds the name in a different phylum, so the taxon itself still needs a taxid |
| `results-filtered.csv` | All records except `MATCH_TAXONOMICALLY_CONSISTENT` |

All input columns are preserved in every output file, with the following ENA columns appended:
| Column | Description |
|---|---|
| `ena_search_term_2` | Actual term searched |
| `ena_search_source_2` | Column used as search source (--man_verify_col) |
| `ena_scientificName_2` | Matched name in ENA database |
| `ena_taxid_2` | ENA taxonomy ID (use this for submissions) |
| `ena_rank_2` | Taxonomic rank in ENA (species, genus, family, etc.) |
| `ena_otherNames_2` | Synonyms/alternative names in ENA (semicolon-separated) |
| `ena_status_2` | Result status code (see Status Codes below) |
| `ena_lineage_2` | Full taxonomic lineage from ENA |

Records in `*-no_ena_matches.csv` still have no ENA match, will proceed to downstream checks, and likely require new taxid minting - proceed to Step 8.



---



### Step 8: GBIF Backbone Search, Round 2 (`05_gbif_ver_check.py`)

**Purpose:** This step runs a second round of GBIF backbone taxonomy searching against the `confirmed_taxonomy` column (the manually reviewed and verified name from Step 6/7). Results are appended to the input as `gbif_*_2` columns. The primary purpose here is to retrieve supporting evidence (GBIF species page URLs, authorship, taxonomic references) that will be used to populate the `description` field in the final ENA taxid request form, created in Step 9.


**Run:**
```bash
python 05_gbif_ver_check.py \
    --input ./taxid_request/04_post_ver_ena_check/04_ena_post_ver-no_ena_matches.csv \
    --output ./taxid_request/05_post_ver_gbif_search/05_post_ver_gbif_search.csv
```

| Argument | Description |
|---|---|
| `-i` / `--input` | Path to the no-ENA-match file from Step 7 (`*-no_ena_matches.csv`; CSV, TSV or XLSX). Must contain `ID`, the search column, `ena_status_2`, `phylum` and `family` (`order` optional) |
| `-o` / `--output` | Path for the output CSV (a timestamped `gbif_ver_check_*.log` is written alongside it) |
| `--search-col` | Column containing the name to search (default: `confirmed_taxonomy`) |

**Logic:**
The search strategy is the same as Step 4, applied to `confirmed_taxonomy` instead of `scientificName`.

For each sample, the script:
1. **Homonym handling**: If `ena_status_2 == TAXONOMIC_MISMATCH_HOMONYM`, a phylum-constrained search is used directly rather than first attempting an unconstrained search. Noted in `gbif_notes_2` as `homonym_resolved_with_phylum`.
2. **Standard search with automatic disambiguation**: Otherwise, queries `confirmed_taxonomy` against the GBIF backbone. If the initial result is too broad (usageKey = 1, rank is `KINGDOM` or `PHYLUM`, or no match is found) and higher taxonomy columns are present, a second disambiguated search is run with `phylum`, `order`, and/or `family` constraints (`disambiguated_with_...`).
3. **Fuzzy alternatives fallback**: If no valid match is found, the script queries GBIF with `verbose=True` to retrieve fuzzy alternative matches. Candidates are filtered by phylum and family where available, then ranked by confidence. The best fuzzy match is used if found, noted in `gbif_notes_2` as `suggested_match_from_fuzzy_alternative` — check these.

A result is considered valid if it has a non-empty `usageKey` (not equal to 1), a `matchType` other than `NONE`, and a rank below `KINGDOM`/`PHYLUM`.

Records are skipped (GBIF columns left as `NOT_FOUND`) if `ena_status_2 == MATCH_TAXONOMICALLY_CONSISTENT` (`skipped_ena_match`) or the search column is empty (`search_term_empty`).

**Output:**
All original input columns are preserved. The following `gbif_*_2` columns are inserted immediately after `ena_lineage_2`:
- `gbif_usageKey_2`, `gbif_scientificName_2`, `gbif_canonicalName_2`, `gbif_rank_2`, `gbif_status_2`, `gbif_matchType_2`, `gbif_confidence_2`, `gbif_kingdom_2`, `gbif_phylum_2`, `gbif_class_2`, `gbif_order_2`, `gbif_family_2`, `gbif_genus_2`, `gbif_species_2`, `gbif_kingdomKey_2`, `gbif_phylumKey_2`, `gbif_classKey_2`, `gbif_orderKey_2`, `gbif_familyKey_2`, `gbif_genusKey_2`, `gbif_speciesKey_2`, `gbif_acceptedUsageKey_2`, `gbif_notes_2`
- Values of `NOT_FOUND` indicate the field was absent or the search failed for that record.
> `gbif_notes_2` values match those possible in Step 4.
> The output CSV from this step feeds into both the final manual verification (Step 9) and the final request form script (Step 10).



---


###  Step 9: Final Manual Verification

**Purpose:** After round 2 of ENA matching and name variation searches using GBIF, one final (simpler) round of manual verification is required. This step is required for those samples where `confirmed_taxonomy` differs from the round 2 GBIF name (`gbif_species_2`), and therefore the GBIF species key cannot be used as evidence in the 'description' column of the final request form.

For these records, check that the **`link`** column holds the supporting evidence: preferably a URL (taxonomic database page, DOI, PDF of the description), otherwise a full literature citation. **The `link` cell is copied as-is into the request form `description`**, so it should contain only the evidence. The `notes` column is for the curator's reasoning and is only used if `link` is empty.

**Examples:**
| confirmed_taxonomy | link (copied to description) | notes (curator reasoning, not used if link is present) |
|---|---|---|
| Cerasma bakhmara | https://opuscula.elte.hu/PDF/Tomus56/Op_Olah_Caucasus_III.pdf | Described in Olah et al 2025 |
| Glyptapanteles aliphera | Fernandez-Triana, J. et al. (2020). Annotated and illustrated world checklist of Microgastrinae parasitoid wasps. ZooKeys 920: 1–1089 | GBIF out of date |
| Tachyura walkeriana | https://www.gbif.org/taxon/... | GBIF out of date, I have followed Lobl & Lobl 2017 |

- Save the (final) manually reviewed and updated `05_post_ver_gbif_search.csv` file from Step 8, and proceed to the final step, Step 10.


---



### Step 10: Final taxid request form genertion (06_final_species_request.py)


**Purpose:** Generates the final ENA taxid request TSV for the remaining species-level records that required manual verification and have no existing ENA entry.

**Run:**
```bash
python 06_final_species_request.py \
    --input ./taxid_request/05_post_ver_gbif_search/05_post_ver_gbif_search-complete.csv \
    --output ./taxid_request/06_final_species_request_form/ \
    --project-id "YOUR_PROJECT_ID"
```

| Argument | Description |
|---|---|
| `--input` | Path to the completed CSV from Step 9 |
| `--output` | Output directory for the request form TSV |
| `--project-id` | Project identifier string for the request form |

**Logic:**
For each record, the script determines the appropriate `description` field for the ENA request form using the following priority order:
1. If `confirmed_taxonomy` matches the GBIF species name returned from round 2 searching (`gbif_species_2`) → uses the GBIF species page URL (`gbif_speciesKey_2`) as evidence
2. If the names differ → copies the whole `link` cell as-is (URL, several URLs, URL plus citation, or a citation on its own). Line breaks/tabs are collapsed to spaces so the TSV stays one row per name
3. If `link` is empty → attempts to extract a structured reference from the `notes` column (e.g. `"Author et al. (Year)"`), falling back to the full `notes` text
4. If `link` and `notes` are both empty → the description is left blank and the record is listed in the log

Required columns: `confirmed_taxonomy`, `gbif_species_2`, `gbif_speciesKey_2`, `type_status`, `ena_status_2`, `link`. The `notes` column is optional (and must be named exactly `notes` to be used).

- The script reports which evidence source was used for each record, lists `link` cells that contain no `http(s)://` URL (citation text used), and flags notes fallbacks and blank descriptions so you can review them before submission.
**- The TSV produced here is the third and final request form. Set it aside for Step 11.**


---


### Step 11 - Merging the generated request forms

- Concatenate the three request form TSV files produced across the pipeline into a single submission file:

| Source | Step | Content |
|---|---|---|
| `non-species_request_form.tsv` | Step 2 | Non-species-level records |
| `*_request_taxid.tsv` (GBIF processor) | Step 5 | Species-level records resolved programmatically |
| Final species request TSV | Step 10 | Species-level records requiring manual verification |

```bash
cat [non-species_request_form.tsv] [first_species_request_form.tsv] [second_species_request_form.tsv] > combined_request_form.tsv
```
- Then, open the file and manually remove the duplicated column headers

**Submit `combined_request_form.tsv` to ENA**
