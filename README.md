# taxid-minting-process
This repository outlines the process of generating taxonomic IDs (taxids) creation request forms from hierarchical taxonomy data, at scale. The pipeline is designed for use in large biodiversity genomics submissions (e.g. UKBOL, BGE) where hundreds to thousands of specimens require validation on whether ENA taxids exist for the taxa before request forms are generated. 

The pipeline handles the full complexity of real-world museum specimen taxonomy: non-species-level identifications, homonyms across kingdoms, synonyms, names absent from ENA, and manual verification edge cases.

<img width="1125" height="362" alt="image" src="https://github.com/user-attachments/assets/88bb0499-d401-4e48-8e71-9cf79cc45e0f" />


---

## Overview

The pipeline takes a CSV (or TSV) of specimen records with hierarchical taxonomic annotations and produces TSV request forms suitable for submission to ENA's taxid minting service. It does this by:

1. Splitting records by identification rank (species vs. non-species)
2. Validating names directly against the ENA taxonomy API
3. Using GBIF as an authority to check taxonomy validity for names not found in ENA
4. Routing records that cannot be resolved programmatically into a structured manual verification step
5. Packaging all resolved names into correctly formatted taxid request forms

The full pipeline involves **8 scripts** and several manual steps. **Step by step details outlining this process can be found in `scripts/README.md**

---

## Requirements

- Python 3.8+
- `pandas`
- `requests`
- `pygbif`
- `openpyxl` (for `.xlsx` output in later steps)

Install dependencies:

```bash
pip install pandas requests pygbif openpyxl
```

Internet access is required for ENA and GBIF API queries. API rate limits are respected internally by each script (ENA: 25 queries/second; GBIF: ~10 queries/second).

---

## Input Data

The pipeline expects a **CSV or TSV file** containing one row per specimen, with the following columns (column names are case-sensitive):

| Column | Required | Description |
|---|---|---|
| `Sample ID` (or permitted alternative) | Yes | Unique specimen/sample identifier |
| `species` | Yes | Species-level identification (leave empty if not identified to species) |
| `genus` | Yes | Genus (leave empty if not identified to genus) |
| `family` | Yes | Family (leave empty if not identified to family) |
| `order` | Yes | Order (leave empty if not identified to order) |
| `class` | Yes | Class (leave empty if not identified to class) |
| `phylum` | Yes | Phylum — critical for homonym resolution |
| `taxid` | No | Pre-existing NCBI taxid (passed through) |
| `matched_rank` | No | Rank of NCBI match (passed through) |
| `lineage` | No | NCBI lineage string (passed through) |
| `lineage_mismatch` | No | Flag for taxonomy conflicts (passed through) |
| `type_status` | Yes | Type specimen status (passed through at this stage but required later) |

> **Note on `phylum`:** Always populate this column where possible. It is the primary mechanism by which the pipeline detects and resolves homonyms - taxonomic names that exist in multiple kingdoms (e.g. a genus name shared between Arthropoda and Rhodophyta). Without phylum, homonym mismatches cannot be detected.


## Troubleshooting

**High rate of `NO_EXACT_MATCH` from ENA.** This is expected for obscure taxa (e.g. parasitoids, rare beetles, microlepidoptera) that are likely not yet registered in ENA. The GBIF search in Step 4 and the GBIF processor in Step 5 handle these programmatically. Truly unresolvable names go to manual verification in Step 6.

**`TAXONOMIC_MISMATCH_HOMONYM` records appearing.** This means a name exists in ENA but under a different phylum than expected. Always check that your input `phylum` column is correctly populated. If the phylum is correct, the GBIF search in Step 4 will use it to resolve the homonym.

**`PHYLUM MISMATCH` / `FAMILY MISMATCH` lines in the GBIF processor log.** The GBIF match sits in a different phylum (any record) or different phylum/class/order/family (GBIF spelling corrections) from the input. These records are sent to manual verification. Either GBIF has matched a homonym, or the input higher taxonomy is wrong (e.g. assigned from a misspelled genus name that belongs to another group) — check which before confirming the name.

**`MATCH_PHYLUM_UNCHECKED` records.** These occur when `phylum` is empty for a record and a match was found in ENA. The match may be correct but cannot be verified. Populate `phylum` and re-run, or review manually.

---

## Script Reference

| Script | Step | Input | Output |
|---|---|---|---|
| `00_filter_taxonomy.py` | 1 | Specimen metadata CSV | `_species.csv`, `_non_species.csv`, `non_species_list.txt` |
| `requester_non-species.py` | 2 | `_non_species.csv` | `non-species_request_form.tsv` |
| `01_ena_taxid_check.py` | 3 | `_species.csv` | ENA-matched and filtered CSVs |
| `02_gbif_backbone_search.py` | 4 | ENA-filtered CSV | GBIF-annotated CSV |
| `03_gbif_name_processor.py` | 5 | GBIF CSV + rules CSV | `_request_taxid.tsv`, `_check_ENA.xlsx`, `_manually_verify.xlsx` |
| `04_post_ver_ena_check.py` | 7 | Completed manual verify XLSX | ENA-matched and filtered CSVs |
| `05_gbif_ver_check.py` | 8 | Post-verification no-match CSV | GBIF-annotated CSV with evidence |
| `06_final_species_request.py` | 10 | Reviewed GBIF CSV | Final species taxid request TSV |


---


## Citation / Acknowledgements

Developed by Dan Parsons and Ben Price at the Natural History Museum (NHM) London, as part of the Biodiversity Genomics Europe (BGE) initiative and UKBOL (UK Barcode of Life) projects.
