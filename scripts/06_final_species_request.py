#!/usr/bin/env python3
"""
TaxID Request Generator

Generates ENA taxonomy request files from annotated input with confirmed taxonomy.

Workflow Context:
    This script is typically run as step 6 in the pipeline:
    1. Run gbif_name_processor.py to generate annotated output with decisions
    2. Review and populate manual verification columns and confirmed_taxonomy
    3. Run 05_gbif_ver_check.py for Round 2 GBIF search (adds gbif_*_2 columns)
    4. Run this script to generate the request_taxid.tsv file for ENA submission

Input Requirements:
    - Accepts CSV, TSV, or XLSX files
    - Must contain columns: confirmed_taxonomy, gbif_species_2, gbif_speciesKey_2,
      type_status, ena_status_2, link
    - Optional column: notes (used only when link is empty)

Processing Logic:
    Records are processed according to the following rules:
    
    1. SKIP: If ena_status_2 == 'MATCH_TAXONOMICALLY_CONSISTENT'
       - Record already has an ENA taxid
    
    2. ERROR: If confirmed_taxonomy is empty
       - Script exits with error listing the offending rows
    
    3. PROCESS: If confirmed_taxonomy == gbif_species_2
       - proposed_name = confirmed_taxonomy
       - description = GBIF URL (https://www.gbif.org/species/{gbif_speciesKey_2})
    
    4. PROCESS: If confirmed_taxonomy != gbif_species_2
       - proposed_name = confirmed_taxonomy
       - description = the whole link cell, as entered during manual verification
         (a URL, several URLs, URL + citation, or a citation without a URL).
         Tabs/line breaks are collapsed to single spaces. Link cells with no
         http(s):// URL are listed in the log.
       - If link is empty: scientific reference extracted from the notes column,
         falling back to the full notes text
       - If link and notes are both empty: description left blank, row logged

    All processed records:
    - name_type = 'published_name'
    - " | TYPE" appended to description if type_status contains 'type'
    - Deduplicated by proposed_name

Output Files:
    {basename}_request_taxid.tsv - Names formatted for ENA taxonomy requests
    {basename}_request_taxid.log - Processing log with skipped records

Usage:
    python 06_final_species_request.py -i INPUT_FILE [-o OUTPUT_DIR] [-p PROJECT_ID]

Examples:
    python 06_final_species_request.py -i input.xlsx
    python 06_final_species_request.py -i input.csv -o ./output/ -p UKBOL

Author: Dan Parsons @NHMUK
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
import pandas as pd


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------

# Regex to capture scientific references like:
#   Fernandez-Triana et al. (2020)
#   Schwarz & Shaw (2000)
#   Lobl & Lobl 2017
#   Broad et al., 2016
#   Morris & Barclay 2017
#   van der Vecht (1959)
#   O'Brien et al. 2021
# Pattern explanation:
#   - Starts with a capital letter (author surname)
#   - Surname can contain hyphens, apostrophes, spaces for particles (van, de, etc.)
#   - Followed by optional additional authors via &, comma, or "et al."
#   - Ends with a 4-digit year, optionally in parentheses
REFERENCE_PATTERN = re.compile(
    r"""
    (?:                                     # optional leading particle
        (?:van\s+(?:den?\s+|der\s+)?|de\s+|von\s+|del?\s+|di\s+|le\s+|la\s+)
    )?
    [A-Z]                                   # first capital letter of surname
    [A-Za-z\u00C0-\u024F''-]+              # rest of surname (accented chars ok)
    (?:                                     # additional authors / et al.
        \s*(?:&|,|and)\s*
        (?:
            (?:et\s+al\.?)                  # et al.
            |
            (?:                             # or another surname
                (?:(?:van\s+(?:den?\s+|der\s+)?|de\s+|von\s+|del?\s+|di\s+|le\s+|la\s+))?
                [A-Z][A-Za-z\u00C0-\u024F''-]+
            )
        )
    )*
    (?:\s+et\s+al\.?)?                      # trailing et al. (if after &/comma author)
    [,.\s]*                                 # optional separator before year
    (?<![A-Za-z0-9])                        # year must not be embedded in a word/number
    \(?\s*(\d{4})\s*\)?                     # year in optional parentheses
    """,
    re.VERBOSE
)


def extract_reference(notes: str) -> tuple:
    """
    Extract a scientific reference from a notes string.
    
    Returns:
        (reference_string, extraction_succeeded)
        If extraction fails, returns (full_notes_string, False)
    """
    if not notes or not notes.strip():
        return ('', False)
    
    match = REFERENCE_PATTERN.search(notes)
    if match:
        ref = match.group(0).strip().rstrip('.,;')
        # Clean mismatched trailing parenthesis (e.g., from "(Broad et al., 2016)")
        if ref.endswith(')') and '(' not in ref:
            ref = ref[:-1].rstrip()
        return (ref, True)
    
    # Fallback: return the full notes text
    return (notes.strip(), False)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def load_input_file(input_file: str) -> pd.DataFrame:
    """Load input file based on extension (CSV, TSV, or XLSX)."""
    input_path = Path(input_file)
    extension = input_path.suffix.lower()
    
    if extension == '.xlsx':
        return pd.read_excel(input_file, dtype=str)
    elif extension == '.tsv':
        return pd.read_csv(input_file, sep='\t', dtype=str)
    elif extension == '.csv':
        return pd.read_csv(input_file, dtype=str)
    else:
        with open(input_file, 'r', encoding='utf-8') as f:
            first_line = f.readline()
            if '\t' in first_line:
                return pd.read_csv(input_file, sep='\t', dtype=str)
            else:
                return pd.read_csv(input_file, dtype=str)


def is_type_specimen(type_status) -> bool:
    """Check if 'type' is present in the type_status value (case-insensitive)."""
    if pd.isna(type_status) or not type_status:
        return False
    return 'type' in str(type_status).lower()


def is_empty(value) -> bool:
    """Check if a value is empty (NaN, None, or empty string)."""
    if pd.isna(value) or value is None:
        return True
    return str(value).strip() == ''


def safe_str(value) -> str:
    """Return stripped string or '' for NaN/None."""
    if pd.isna(value) or value is None:
        return ''
    return str(value).strip()


URL_PATTERN = re.compile(r'https?://', re.IGNORECASE)


def clean_link(value) -> str:
    """Return the link cell as a single line: tabs/line breaks collapsed to single spaces."""
    return ' '.join(safe_str(value).split())


def get_gbif_url(gbif_key: str) -> str:
    """Generate GBIF species URL from key."""
    key = safe_str(gbif_key)
    if key and key.upper() != 'NOT_FOUND':
        return f"https://www.gbif.org/species/{key}"
    return ''


class Logger:
    """Simple logger that writes to both console and file."""
    
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.messages = []
        
    def log(self, message: str, console: bool = True):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}"
        self.messages.append(log_entry)
        if console:
            print(message)
    
    def write(self):
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.messages))


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_file(input_file: str, output_dir: str = None, project_id: str = 'BGE'):
    """
    Process the annotated input file and generate request_taxid.tsv.
    
    Args:
        input_file: Path to annotated input file (CSV/TSV/XLSX)
        output_dir: Output directory (default: same as input)
        project_id: Project ID for taxonomy requests
    """
    input_path = Path(input_file)
    basename = input_path.stem
    
    # Remove common suffixes for cleaner output name
    for suffix in ['_annotated', '_processed', '_output']:
        if basename.endswith(suffix):
            basename = basename[:-len(suffix)]
            break
    
    if output_dir:
        out_path = Path(output_dir)
    else:
        out_path = input_path.parent
    
    log_file = out_path / f"{basename}_request_taxid.log"
    logger = Logger(log_file)
    
    # Load input
    logger.log(f"Loading: {input_file}")
    df = load_input_file(input_file)
    logger.log(f"  Loaded {len(df)} rows")
    
    # Validate required columns
    required_columns = [
        'confirmed_taxonomy', 'gbif_species_2', 'gbif_speciesKey_2',
        'type_status', 'ena_status_2', 'link'
    ]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        logger.log(f"Error: Missing required columns: {missing_columns}")
        logger.write()
        sys.exit(1)
    if 'notes' not in df.columns:
        logger.log("  No 'notes' column found - rows with an empty link will get a blank description")
    
    # ID column for logging
    id_col = None
    for candidate in ['ID', 'id', 'sample_id', 'specimen_id']:
        if candidate in df.columns:
            id_col = candidate
            break
    
    # ---------------------------------------------------------------------------
    # Pass 1 — check for empty confirmed_taxonomy (after ENA skip filter)
    # ---------------------------------------------------------------------------
    empty_ct_rows = []
    for idx, row in df.iterrows():
        ena_status_2 = safe_str(row['ena_status_2'])
        if ena_status_2 == 'MATCH_TAXONOMICALLY_CONSISTENT':
            continue
        ct = safe_str(row['confirmed_taxonomy'])
        if not ct:
            row_id = safe_str(row[id_col]) if id_col else f"row_{idx}"
            sci_name = safe_str(row.get('scientificName', ''))
            empty_ct_rows.append(f"  [{row_id}] {sci_name}" if sci_name else f"  [{row_id}]")
    
    if empty_ct_rows:
        logger.log("ERROR: The following rows have empty confirmed_taxonomy and are not skipped by ENA match:")
        for r in empty_ct_rows:
            logger.log(r)
        logger.log("Please review and populate confirmed_taxonomy for these rows before rerunning.")
        logger.write()
        sys.exit(1)
    
    # ---------------------------------------------------------------------------
    # Pass 2 — process records
    # ---------------------------------------------------------------------------
    processed_names = {}  # key: proposed_name -> {description, processing_type}
    
    skipped_ena = []
    matched_gbif = []          # CT == gbif_species_2
    used_link = []              # CT != gbif_species_2, link cell used
    link_no_url = []            # subset of used_link: link cell has no http(s):// URL
    used_reference = []         # CT != gbif_species_2, link empty, reference extracted from notes
    used_notes_fallback = []    # CT != gbif_species_2, link empty, full notes used
    empty_description = []      # no description could be built (warnings)
    no_evidence = []            # CT != gbif_species_2, link and notes both empty

    for idx, row in df.iterrows():
        row_id = safe_str(row[id_col]) if id_col else f"row_{idx}"
        confirmed_taxonomy = safe_str(row['confirmed_taxonomy'])
        gbif_species_2 = safe_str(row['gbif_species_2'])
        gbif_speciesKey_2 = safe_str(row['gbif_speciesKey_2'])
        ena_status_2 = safe_str(row['ena_status_2'])
        link = clean_link(row['link'])
        notes = safe_str(row.get('notes', ''))
        type_status = safe_str(row.get('type_status', ''))
        is_type = is_type_specimen(type_status)
        
        # Rule 1: Skip if ENA match found
        if ena_status_2 == 'MATCH_TAXONOMICALLY_CONSISTENT':
            sci_name = safe_str(row.get('scientificName', confirmed_taxonomy))
            logger.log(f"SKIPPED [{row_id}] {sci_name}: ENA taxid already found (MATCH_TAXONOMICALLY_CONSISTENT)")
            skipped_ena.append(row_id)
            continue
        
        # Determine description
        proposed_name = confirmed_taxonomy
        description = ''
        processing_type = ''
        
        if confirmed_taxonomy == gbif_species_2:
            # CT matches GBIF — use GBIF URL
            description = get_gbif_url(gbif_speciesKey_2)
            processing_type = 'gbif_match'
            matched_gbif.append(row_id)
            if not description:
                empty_description.append(
                    f"[{row_id}] {confirmed_taxonomy}: CT matches gbif_species_2 but no valid gbif_speciesKey_2"
                )
        elif link:
            # CT differs from GBIF — use the curator's link cell as-is
            description = link
            processing_type = 'link'
            used_link.append(row_id)
            if not URL_PATTERN.search(link):
                link_no_url.append(f"[{row_id}] {confirmed_taxonomy}: {link}")
            logger.log(f"  [{row_id}] {confirmed_taxonomy}: link used → {link}", console=False)
        else:
            # CT differs from GBIF and link is empty — extract reference from notes
            ref, extracted = extract_reference(notes)
            if extracted:
                description = ref
                processing_type = 'reference_extracted'
                used_reference.append(row_id)
                logger.log(
                    f"  [{row_id}] {confirmed_taxonomy}: extracted reference → {ref}",
                    console=False
                )
            elif ref:
                # Fallback to full notes text
                description = ref
                processing_type = 'notes_fallback'
                used_notes_fallback.append(row_id)
                logger.log(
                    f"  [{row_id}] {confirmed_taxonomy}: no reference pattern found, using full notes",
                    console=False
                )
            else:
                processing_type = 'no_description'
                no_evidence.append(row_id)
                empty_description.append(
                    f"[{row_id}] {confirmed_taxonomy}: CT differs from gbif_species_2 and link and notes are empty"
                )
        
        # TYPE annotation
        if is_type:
            description = f"{description} | TYPE" if description else "TYPE"
        
        # Deduplicate by proposed_name (preserve TYPE if any duplicate is a type)
        if proposed_name in processed_names:
            if is_type and '| TYPE' not in processed_names[proposed_name]['description']:
                existing = processed_names[proposed_name]['description']
                processed_names[proposed_name]['description'] = (
                    f"{existing} | TYPE" if existing else "TYPE"
                )
        else:
            processed_names[proposed_name] = {
                'description': description,
                'processing_type': processing_type
            }
    
    # ---------------------------------------------------------------------------
    # Build and write output
    # ---------------------------------------------------------------------------
    output_rows = []
    for proposed_name, info in processed_names.items():
        output_rows.append({
            'proposed_name': proposed_name,
            'name_type': 'published_name',
            'host': '',
            'project_id': project_id,
            'description': info['description']
        })
    
    output_file = out_path / f"{basename}_request_taxid.tsv"
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['proposed_name', 'name_type', 'host', 'project_id', 'description'],
            delimiter='\t'
        )
        writer.writeheader()
        writer.writerows(output_rows)
    
    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    logger.log(f"\n{'='*60}")
    logger.log("PROCESSING SUMMARY")
    logger.log(f"{'='*60}")
    logger.log(f"Input file: {input_file}")
    logger.log(f"Total rows in input: {len(df)}")
    logger.log(f"\nSkipped — ENA taxid found: {len(skipped_ena)}")
    
    logger.log(f"\nProcessed records by description source:")
    no_description = len(no_evidence)
    logger.log(f"  CT == gbif_species_2 (GBIF URL):                    {len(matched_gbif)}")
    logger.log(f"  CT != gbif_species_2 (link):                        {len(used_link)}")
    logger.log(f"    of which link has no URL (citation text used):    {len(link_no_url)}")
    logger.log(f"  CT != gbif_species_2, no link (ref from notes):     {len(used_reference)}")
    logger.log(f"  CT != gbif_species_2, no link (full notes text):    {len(used_notes_fallback)}")
    logger.log(f"  CT != gbif_species_2, no link or notes (blank):     {no_description}")

    total_processed = (len(matched_gbif) + len(used_link) + len(used_reference)
                       + len(used_notes_fallback) + no_description)
    logger.log(f"\nTotal processed: {total_processed}")
    logger.log(f"Unique names in output: {len(output_rows)}")
    logger.log(f"Type specimens: {sum(1 for r in output_rows if '| TYPE' in r.get('description', ''))}")
    
    if link_no_url:
        logger.log(f"\nLink has no http(s):// URL — citation text used as description ({len(link_no_url)}):")
        for w in link_no_url:
            logger.log(f"    - {w}", console=False)

    if used_notes_fallback:
        logger.log(f"\nNotes fallback — no reference pattern found ({len(used_notes_fallback)}):")
        logger.log("  (Full notes text was used as description — review for accuracy)")
        for row_id in used_notes_fallback:
            logger.log(f"    - {row_id}", console=False)
    
    if empty_description:
        logger.log(f"\nWarnings — missing description ({len(empty_description)}):")
        for w in empty_description:
            logger.log(f"  {w}")
    
    logger.log(f"\nOutput files:")
    logger.log(f"  {output_file}")
    logger.log(f"  {log_file}")
    
    logger.write()
    print(f"\nProcessing complete. See {log_file} for full details.")


def main():
    parser = argparse.ArgumentParser(
        description='Generate ENA taxonomy request file from annotated input with confirmed taxonomy.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Input:
  Accepts CSV, TSV, or XLSX files (auto-detected by extension)
  Must contain: confirmed_taxonomy, gbif_species_2, gbif_speciesKey_2,
                type_status, ena_status_2, link
  Optional:     notes (used only when link is empty)

Output:
  {basename}_request_taxid.tsv - Names formatted for ENA taxonomy requests
  {basename}_request_taxid.log - Processing log with details

Processing Rules:
  1. SKIP if ena_status_2 == 'MATCH_TAXONOMICALLY_CONSISTENT'
  2. ERROR if confirmed_taxonomy is empty (for non-skipped rows)
  3. If confirmed_taxonomy == gbif_species_2: description = GBIF URL
  4. If confirmed_taxonomy != gbif_species_2: description = link cell as-is
     5. If link is empty: scientific reference extracted from notes
        (falls back to full notes text if no reference found)
     6. If link and notes are empty: blank description, row listed in log

Examples:
  python 06_final_species_request.py -i results_annotated.xlsx -p BGE
  python 06_final_species_request.py -i results_annotated.csv -o ./output -p UKBOL
        """
    )
    parser.add_argument('-i', '--input', required=True,
                        help='Input file (CSV/TSV/XLSX)')
    parser.add_argument('-o', '--output-dir',
                        help='Output directory (default: same as input)')
    parser.add_argument('-p', '--project-id', default='BGE',
                        help='Project ID for taxonomy requests (default: BGE)')
    
    args = parser.parse_args()
    
    if not os.path.isfile(args.input):
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
    
    process_file(args.input, args.output_dir, args.project_id)


if __name__ == '__main__':
    main()