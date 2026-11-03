#!/usr/bin/env python3
"""
ENA Taxonomy ID Checker (04_ena_taxid_check.py)
===============================================

This script validates taxonomic names against the European Nucleotide Archive (ENA) 
taxonomy database and retrieves corresponding taxon IDs for sequence submission.

This script is designed to process the output of the manual review step, where all
rows have been reviewed and a confirmed_taxonomy column has been populated with the
correct name to search in ENA.

WORKFLOW CONTEXT
----------------
1. Run 03_gbif_name_processor.py to generate _manually_verify.xlsx (and _annotated.xlsx)
2. Manually review rows and populate the confirmed_taxonomy column (the reviewed
   sheet may be deduplicated to one row per species)
3. Run this script to validate names against ENA and retrieve taxon IDs

OVERVIEW
--------
The script takes a spreadsheet of specimen records, selects rows to process,
queries the ENA taxonomy API using the column specified by --man_verify_col
(default: confirmed_taxonomy), and outputs validated taxonomy data with ENA
taxon IDs. Results are split into separate files based on match quality to
facilitate downstream processing.

ROW SELECTION LOGIC
-------------------
The decision flag columns from 03_gbif_name_processor.py (name_to_use,
check_ENA_with_GBIF, manual_verification_needed) are OPTIONAL.

If the flag columns are NOT present (e.g. a reviewed _manually_verify.xlsx with
the decision columns removed), every row is processed and the search term is
always taken from --man_verify_col.

If ALL three flag columns are present (e.g. _manually_verify.xlsx with decision
columns, or _annotated.xlsx), only rows meeting ONE of the following two
conditions are processed and included in any output file. All other rows are
excluded.

Condition 1 - Manual verification rows:
    manual_verification_needed == 'yes'
    Search term: value from --man_verify_col column

Condition 2 - GBIF name ENA check rows (GBIF spelling corrections):
    manual_verification_needed != 'yes'
    AND name_to_use == 'gbif'
    AND check_ENA_with_GBIF == 'yes'
    Search term: value from --man_verify_col if populated, otherwise gbif_species

If neither condition is met, the row is excluded from all outputs entirely.
If only some of the flag columns are present, a warning is issued and the input
is treated as if no flag columns were present.

INPUT REQUIREMENTS
------------------
Supported file formats: CSV, TSV, XLSX

Required columns:
    - <man_verify_col>: Name to search in ENA (default: confirmed_taxonomy). A warning
      is issued for any selected row where the search term is empty.

Optional flag columns (from 03_gbif_name_processor.py output):
    - manual_verification_needed, name_to_use, check_ENA_with_GBIF
    
Recommended columns for homonym disambiguation:
    - phylum: Used to verify matches against ENA lineage (critical for resolving homonyms)
    
Optional columns (passed through to output):
    - ID: Sample/specimen identifier
    - scientificName: Original taxonomic name
    - And any other columns from the input file

SEARCH LOGIC
------------
The script:
1. Selects rows meeting Condition 1 or Condition 2 (see ROW SELECTION LOGIC)
2. Warns (but continues) if any selected row has an empty confirmed_taxonomy
3. Searches ENA using the specified column
4. Outputs only selected rows with ENA columns appended

LOGICAL PROCESS
---------------
For each selected row:

1. GET SEARCH TERM
   - Uses the value from the specified column (--man_verify_col)
   - Removes 'cf.' qualifiers if present (e.g., "Genus cf. species" -> "Genus species")
   - If search term is empty/missing, warns and marks as MAN_VER_NAME_EMPTY

2. QUERY ENA API
   - Calls: https://www.ebi.ac.uk/ena/taxonomy/rest/suggest-for-submission/{name}
   - Rate limited to 25 queries/second to respect API limits
   - Retries up to 3 times on failure with 2-second delays

3. FIND EXACT MATCHES
   - Compares search term against returned 'scientificName' fields (case-insensitive)
   - Also checks 'otherNames' field for synonyms
   - If no exact match found, marks as NO_EXACT_MATCH

4. VERIFY TAXONOMIC CONSISTENCY (Homonym Detection)
   - For each exact match, fetches full taxonomy via:
     https://www.ebi.ac.uk/ena/taxonomy/rest/tax-id/{taxid}
   - If input has 'phylum' column, checks if phylum appears in ENA lineage
   - Matches with wrong phylum are flagged as homonyms

5. SELECT BEST MATCH
   - Prioritises matches where phylum was verified
   - Falls back to unverified matches if no phylum provided in input

OUTPUT FILES
------------
The script produces a combined output file plus five category-specific files.
Only rows selected for processing are included in any output file.
If --output is 'results.csv', the following files are created:

1. results.csv
   - Combined output containing all selected records with ENA columns appended
   
2. results-ena_matches.csv
   - Status: MATCH_TAXONOMICALLY_CONSISTENT
   - Records where ENA returned an exact match AND the phylum was verified
   - These are ready for ENA submission
   
3. results-errors.csv
   - Status: MAN_VER_NAME_EMPTY, API_ERROR:*
   - Records that couldn't be processed due to missing data or API failures
   
4. results-possible_homonym.csv
   - Status: MATCH_PHYLUM_UNCHECKED, TAXONOMIC_MISMATCH_HOMONYM
   - Requires manual review to confirm correct taxon ID

5. results-no_ena_matches.csv
   - Status: NO_EXACT_MATCH, NO_RESULTS_RETURNED, TAXONOMIC_MISMATCH_HOMONYM
   - May indicate spelling errors, synonyms not in ENA, or taxa not yet in database
   - TAXONOMIC_MISMATCH_HOMONYM rows are written to BOTH this file and
     possible_homonym.csv: ENA only holds the name in a different phylum, so the
     taxon itself has no ENA taxid and continues to 05_gbif_ver_check.py

6. results-filtered.csv
   - All selected records EXCEPT those with MATCH_TAXONOMICALLY_CONSISTENT status

OUTPUT COLUMNS
--------------
All input columns are preserved, plus the following ENA columns are appended:

    ena_search_term_2: The actual term searched (after cf. removal)
    ena_search_source_2: Column used for search
    ena_scientificName_2: Matched name in ENA database
    ena_taxid_2: ENA taxonomy ID (use this for submissions)
    ena_rank_2: Taxonomic rank in ENA (species, genus, family, etc.)
    ena_otherNames_2: Synonyms/alternative names in ENA (semicolon-separated)
    ena_status_2: Result status code (see STATUS CODES below)
    ena_lineage_2: Full taxonomic lineage from ENA

STATUS CODES
------------
    MATCH_TAXONOMICALLY_CONSISTENT: Exact match found, phylum verified
    MATCH_PHYLUM_UNCHECKED: Exact match found, but no phylum to verify
    TAXONOMIC_MISMATCH_HOMONYM: Exact match but phylum mismatch (wrong kingdom/phylum)
    NO_EXACT_MATCH: API returned results but none matched exactly
    NO_RESULTS_RETURNED: API returned empty results
    MAN_VER_NAME_EMPTY: No search name provided in selected row (warning issued)
    API_ERROR: *: Various API communication errors (includes error details)

USAGE
-----
    python 04_ena_taxid_check.py --input annotated.xlsx --output results.csv
    python 04_ena_taxid_check.py -i annotated.xlsx -o results.csv -mv confirmed_taxonomy
    
    Arguments:
        --input, -i         : Path to input file (CSV, TSV, or XLSX)
        --output, -o        : Path to output CSV file (split files use same base name)
        --man_verify_col, -mv : Column name to use as ENA search term (default: confirmed_taxonomy)

LOGGING
-------
A log file (04_ena_taxid_check.log) is created in the output directory containing:
    - Row selection summary
    - API calls made
    - Match details and decisions
    - Summary statistics
    - Any warnings or errors

Author: Dan Parsons @NHMUK
"""

import os
import sys
import pandas as pd
from pathlib import Path
import pathlib
import logging
import csv
import argparse
import requests
import urllib.parse
import time

# Configure logger
logger = logging.getLogger()
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.setLevel(logging.INFO)


def setup_logging(output_path):
    """Set up file logging in the output directory"""
    output_dir = os.path.dirname(output_path)
    
    # Create output directory if it doesn't exist
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
        logger.info(f"Created output directory: {output_dir}")
    
    log_file = os.path.join(output_dir, "04_ena_taxid_check.log") if output_dir else "04_ena_taxid_check.log"
    file_handler = logging.FileHandler(log_file)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info(f"Logging to: {log_file}")


def read_file(file_path):
    """Read input file (CSV, TSV, or XLSX)"""
    if not Path(file_path).is_file():
        logger.error(f"Error: The file '{file_path}' does not exist.")
        sys.exit(1)
    
    file_suffix = pathlib.Path(file_path).suffix.lower()
    
    if file_suffix == ".xlsx":
        logger.info("Reading Excel file")
        df = pd.read_excel(file_path)
    elif file_suffix == ".tsv":
        logger.info("Reading TSV file (tab-delimited)")
        df = pd.read_csv(file_path, delimiter='\t')
    elif file_suffix == ".csv":
        logger.info("Reading CSV file (comma-delimited)")
        df = pd.read_csv(file_path, delimiter=',')
    else:
        logger.error(f"Unsupported file extension '{file_suffix}'. Supported formats: .csv, .tsv, .xlsx")
        sys.exit(1)
    
    # Drop rows that are entirely empty
    df.dropna(how="all", inplace=True)
    return df


def is_empty_value(value):
    """Check if a value is empty, NaN, or 'not collected'"""
    if pd.isna(value):
        return True
    str_val = str(value).strip().lower()
    return str_val in ('', 'nan', 'not collected', 'none')


FLAG_COLUMNS = ['manual_verification_needed', 'name_to_use', 'check_ENA_with_GBIF']


def has_flag_columns(df):
    """
    Return True if all three decision flag columns from 03_gbif_name_processor.py
    are present. Warn if only some of them are present.
    """
    present = [c for c in FLAG_COLUMNS if c in df.columns]
    if len(present) == len(FLAG_COLUMNS):
        return True
    if present:
        missing = [c for c in FLAG_COLUMNS if c not in df.columns]
        logger.warning(
            f"Only some decision flag columns are present (found {present}, missing {missing}). "
            f"Ignoring the flags: every row will be processed using the search column."
        )
    return False


def select_rows(df, flags_present):
    """
    Select rows to process.

    If the decision flag columns are not present, all rows are selected.

    Otherwise, rows meeting either of the two processing conditions are selected:
    Condition 1: manual_verification_needed == 'yes'
    Condition 2: manual_verification_needed != 'yes'
                 AND name_to_use == 'gbif'
                 AND check_ENA_with_GBIF == 'yes'

    Returns a filtered DataFrame containing only selected rows, and logs
    a breakdown of how many rows were selected per condition and excluded.
    """
    if not flags_present:
        logger.info(
            "Decision flag columns (manual_verification_needed, name_to_use, check_ENA_with_GBIF) "
            "not found - treating input as a manually verified sheet."
        )
        logger.info(f"Row selection summary:")
        logger.info(f"  Total rows in input / selected for processing: {len(df)}")
        return df.copy()

    # Normalise flag columns to lowercase strings for comparison
    mvn = df['manual_verification_needed'].fillna('').astype(str).str.strip().str.lower()
    ntu = df['name_to_use'].fillna('').astype(str).str.strip().str.lower()
    ceg = df['check_ENA_with_GBIF'].fillna('').astype(str).str.strip().str.lower()

    cond1 = mvn == 'yes'
    cond2 = (mvn != 'yes') & (ntu == 'gbif') & (ceg == 'yes')
    selected = cond1 | cond2

    n_cond1 = cond1.sum()
    n_cond2 = cond2.sum()
    n_excluded = (~selected).sum()

    logger.info(f"Row selection summary:")
    logger.info(f"  Total rows in input:                     {len(df)}")
    logger.info(f"  Condition 1 (manual_verification_needed=yes): {n_cond1} rows")
    logger.info(f"  Condition 2 (name_to_use=gbif + check_ENA=yes, not manual): {n_cond2} rows")
    logger.info(f"  Total selected for processing:           {selected.sum()}")
    logger.info(f"  Excluded (not meeting either condition): {n_excluded} rows")

    return df[selected].copy()


def search_ena_taxonomy(taxon_name, higher_taxonomy, logger):
    """
    Search ENA taxonomy database for a taxon name and verify with higher taxonomy.
    
    Parameters:
    - taxon_name: The name to search for
    - higher_taxonomy: dict containing phylum, class, order, family as available
    
    Returns dict with ENA fields or error information
    """
    result = {
        'ena_scientificName_2': '',
        'ena_taxid_2': '',
        'ena_displayName_2': '',
        'ena_rank_2': '',
        'ena_otherNames_2': '',
        'ena_commonName_2': '',
        'ena_status_2': '',
        'ena_lineage_2': ''
    }
    
    if not taxon_name:
        result['ena_status_2'] = 'MAN_VER_NAME_EMPTY'
        return result
    
    # Clean taxon name - remove "cf." qualifier
    clean_taxon_name = taxon_name
    if " cf. " in clean_taxon_name:
        clean_taxon_name = clean_taxon_name.replace(" cf. ", " ")
        logger.info(f"Removed 'cf.' qualifier from taxon name: '{taxon_name}' -> '{clean_taxon_name}'")
    
    # URL encode the cleaned taxon name
    encoded_name = urllib.parse.quote(clean_taxon_name)
    api_url = f'https://www.ebi.ac.uk/ena/taxonomy/rest/suggest-for-submission/{encoded_name}'
    
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Calling ENA API: {api_url}")
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            
            results = response.json()
            
            # No results found
            if not results:
                logger.info(f"No results found for '{clean_taxon_name}'")
                result['ena_status_2'] = 'NO_RESULTS_RETURNED'
                return result
            
            logger.info(f"Found {len(results)} potential matches from ENA for '{clean_taxon_name}'")
            
            # First, find exact matches
            taxon_name_lower = clean_taxon_name.lower()
            exact_matches = []
            
            # Log all matches for debugging
            logger.info(f"Checking all {len(results)} matches for '{clean_taxon_name}':")
            
            for idx, taxon in enumerate(results):
                sci_name = taxon.get('scientificName', '')
                tax_id = taxon.get('taxId', '')
                display_name = taxon.get('displayName', '')
                
                logger.info(f"  Match {idx+1}: scientificName='{sci_name}', taxId={tax_id}, displayName='{display_name}'")
                
                # Check for direct match with scientificName
                is_match = False
                if sci_name.lower() == taxon_name_lower:
                    logger.info(f"  -> EXACT MATCH found in scientificName")
                    is_match = True
                
                # Check in otherNames for exact match
                if not is_match:
                    other_names = taxon.get('otherNames', [])
                    if other_names:
                        logger.info(f"     otherNames: {other_names}")
                        
                    for other_name in other_names:
                        # Extract the name part before any colon
                        name_parts = other_name.split(':', 1)
                        other_name_clean = name_parts[0].strip()
                        
                        if other_name_clean.lower() == taxon_name_lower:
                            logger.info(f"  -> EXACT MATCH found in otherNames: '{other_name}'")
                            is_match = True
                            break
                
                if is_match:
                    exact_matches.append(taxon)
            
            # If no exact matches, return no match
            if not exact_matches:
                logger.warning(f"No exact match found among {len(results)} results for '{clean_taxon_name}'")
                result['ena_status_2'] = 'NO_EXACT_MATCH'
                return result
            
            # For each match, verify taxonomic consistency
            valid_matches = []
            homonyms = []
            
            for match in exact_matches:
                tax_id = match.get('taxId', '')
                
                # Skip if no tax_id
                if not tax_id:
                    continue
                
                # Get lineage from ENA taxonomy API
                try:
                    lineage_url = f'https://www.ebi.ac.uk/ena/taxonomy/rest/tax-id/{tax_id}'
                    lineage_response = requests.get(lineage_url, timeout=10)
                    lineage_response.raise_for_status()
                    taxon_data = lineage_response.json()
                    
                    # Get lineage as string
                    lineage_str = taxon_data.get('lineage', '')
                    logger.info(f"Lineage for taxId {tax_id}: {lineage_str}")
                    
                    # Check if phylum matches
                    is_consistent = True
                    phylum_matched = False
                    is_homonym = False

                    if 'phylum' in higher_taxonomy and higher_taxonomy['phylum'] and 'lineage' in taxon_data:
                        phylum_value = higher_taxonomy['phylum'].lower()
                        lineage_lower = taxon_data.get('lineage', '').lower()
    
                        if phylum_value in lineage_lower:
                            logger.info(f"  Phylum match confirmed: '{phylum_value}' found in ENA lineage")
                            phylum_matched = True
                        else:
                            logger.info(f"  Phylum NOT found in lineage: Input phylum '{phylum_value}' not found in '{lineage_lower}'")
                            is_homonym = True
                            is_consistent = False        
                    
                    match_data = {
                        'taxon': match,
                        'lineage': lineage_str,
                        'phylum_matched': phylum_matched,
                        'is_homonym': is_homonym
                    }
                    
                    if is_homonym:
                        homonyms.append(match_data)
                    elif is_consistent:
                        valid_matches.append(match_data)
                        
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Failed to get taxonomy for taxId {tax_id}: {str(e)}")
            
            # First check if we found homonyms
            if homonyms and not valid_matches:
                logger.warning(f"All {len(homonyms)} matches were taxonomic homonyms (different phylum)")
                result['ena_status_2'] = 'TAXONOMIC_MISMATCH_HOMONYM'
                return result
            
            # Process valid matches
            if valid_matches:
                phylum_matches = [m for m in valid_matches if m['phylum_matched']]
                
                if phylum_matches:
                    chosen_match = phylum_matches[0]['taxon']
                    result['ena_status_2'] = 'MATCH_TAXONOMICALLY_CONSISTENT'
                    result['ena_lineage_2'] = phylum_matches[0]['lineage']
                else:
                    chosen_match = valid_matches[0]['taxon']
                    result['ena_status_2'] = 'MATCH_PHYLUM_UNCHECKED'
                    result['ena_lineage_2'] = valid_matches[0]['lineage']
                
                result['ena_scientificName_2'] = chosen_match.get('scientificName', '')
                result['ena_taxid_2'] = str(chosen_match.get('taxId', ''))
                result['ena_displayName_2'] = chosen_match.get('displayName', '')
                result['ena_rank_2'] = chosen_match.get('rank', '')
                result['ena_commonName_2'] = chosen_match.get('commonName', '')
                
                other_names_list = chosen_match.get('otherNames', [])
                result['ena_otherNames_2'] = '; '.join(other_names_list) if other_names_list else ''
                
                return result
            else:
                if homonyms:
                    result['ena_status_2'] = 'TAXONOMIC_MISMATCH_HOMONYM'
                else:
                    result['ena_status_2'] = 'API_ERROR: Lineage lookup failed for all matches'
                return result
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                logger.warning(f"API request failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                continue
            logger.error(f"API request failed after {max_retries} attempts: {str(e)}")
            result['ena_status_2'] = f'API_ERROR: {str(e)}'
            return result
        
        except (ValueError, KeyError) as e:
            logger.error(f"API response parsing error: {str(e)}")
            result['ena_status_2'] = f'API_ERROR: Response parsing error - {str(e)}'
            return result
    
    result['ena_status_2'] = 'API_ERROR: Maximum retries exceeded'
    return result


def get_search_column(row, man_verify_col, flags_present):
    """
    Decide which column supplies the ENA search term for a row.

    - No flag columns: always man_verify_col
    - Condition 1 (manual_verification_needed == 'yes'): man_verify_col
    - Condition 2 (GBIF spelling correction): man_verify_col if it has been
      populated during manual verification, otherwise gbif_species
    """
    if not flags_present:
        return man_verify_col
    mvn = str(row.get('manual_verification_needed', '')).strip().lower()
    if mvn == 'yes':
        return man_verify_col
    if not is_empty_value(row.get(man_verify_col, '')):
        return man_verify_col
    return 'gbif_species'


def process_dataframe(df, man_verify_col, flags_present, logger):
    """
    Process all rows in the (pre-filtered) dataframe, searching ENA taxonomy for each.
    Rows with an empty search column are warned about and marked MAN_VER_NAME_EMPTY.
    Appends ENA columns to all rows.
    """
    # Initialize ENA columns with empty values
    ena_columns = {
        'ena_search_term_2': [''] * len(df),
        'ena_search_source_2': [''] * len(df),
        'ena_scientificName_2': [''] * len(df),
        'ena_taxid_2': [''] * len(df),
        'ena_displayName_2': [''] * len(df),
        'ena_rank_2': [''] * len(df),
        'ena_otherNames_2': [''] * len(df),
        'ena_commonName_2': [''] * len(df),
        'ena_status_2': [''] * len(df),
        'ena_lineage_2': [''] * len(df)
    }
    
    # 40ms = 25 queries per second
    min_delay = 0.04
    last_request_time = 0
    
    # Track processing stats
    processed_count = 0
    empty_count = 0

    # Use enumerate over iterrows to map back to the ena_columns lists
    # (df index may not be 0-based after filtering)
    rows = list(df.iterrows())

    # Pre-flight check: warn about any selected rows with empty search column
    empty_rows = []
    for list_idx, (df_idx, row) in enumerate(rows):
        check_col = get_search_column(row, man_verify_col, flags_present)
        search_val = row.get(check_col, '')
        if is_empty_value(search_val):
            sample_id = row.get('ID', df_idx)
            empty_rows.append((list_idx, df_idx, sample_id, check_col))

    if empty_rows:
        logger.warning(
            f"WARNING: {len(empty_rows)} selected row(s) have an empty search column "
            f"and will be marked as MAN_VER_NAME_EMPTY:"
        )
        for list_idx, df_idx, sample_id, check_col in empty_rows:
            logger.warning(f"  Row index {df_idx} (ID={sample_id}, search column='{check_col}')")
    else:
        logger.info(f"All selected rows have a non-empty search column - good.")

    for list_idx, (df_idx, row) in enumerate(rows):
        # Pick the search column (see get_search_column for the rules)
        search_col = get_search_column(row, man_verify_col, flags_present)

        search_term = row.get(search_col, '')
        if not is_empty_value(search_term):
            search_term = str(search_term).strip()
        else:
            search_term = None
        
        # Collect higher taxonomy for homonym disambiguation
        higher_taxonomy = {}
        for level in ['phylum', 'class', 'order', 'family', 'genus']:
            if level in row and pd.notna(row[level]) and str(row[level]).strip().lower() not in ('', 'not collected', 'nan'):
                higher_taxonomy[level] = str(row[level]).strip()
        
        sample_id = row.get('ID', df_idx)
        logger.info(f"Row {df_idx} (ID={sample_id}): searching '{search_col}' = '{search_term}'")
        
        # Rate limiting
        current_time = time.time()
        time_since_last = current_time - last_request_time
        if time_since_last < min_delay:
            time.sleep(min_delay - time_since_last)
        
        last_request_time = time.time()
        
        # Search ENA
        if search_term:
            result = search_ena_taxonomy(search_term, higher_taxonomy, logger)
            ena_columns['ena_search_term_2'][list_idx] = search_term
            processed_count += 1
        else:
            result = {
                'ena_scientificName_2': '',
                'ena_taxid_2': '',
                'ena_displayName_2': '',
                'ena_rank_2': '',
                'ena_otherNames_2': '',
                'ena_commonName_2': '',
                'ena_status_2': 'MAN_VER_NAME_EMPTY',
                'ena_lineage_2': ''
            }
            ena_columns['ena_search_term_2'][list_idx] = ''
            empty_count += 1
        
        # Store results
        ena_columns['ena_search_source_2'][list_idx] = search_col
        ena_columns['ena_scientificName_2'][list_idx] = result['ena_scientificName_2']
        ena_columns['ena_taxid_2'][list_idx] = result['ena_taxid_2']
        ena_columns['ena_displayName_2'][list_idx] = result['ena_displayName_2']
        ena_columns['ena_rank_2'][list_idx] = result['ena_rank_2']
        ena_columns['ena_otherNames_2'][list_idx] = result['ena_otherNames_2']
        ena_columns['ena_commonName_2'][list_idx] = result['ena_commonName_2']
        ena_columns['ena_status_2'][list_idx] = result['ena_status_2']
        ena_columns['ena_lineage_2'][list_idx] = result.get('ena_lineage_2', '')
    
    logger.info(
        f"Processing complete: {processed_count} rows searched, "
        f"{empty_count} rows skipped (empty search column)"
    )
    
    # Append ENA columns to dataframe using positional assignment
    for col_name, col_data in ena_columns.items():
        df[col_name] = col_data
    
    return df


def get_output_filepath(base_output_path, suffix):
    """
    Generate output filepath with suffix inserted before the extension.
    e.g., 'results.csv' with suffix '-ena_matches' becomes 'results-ena_matches.csv'
    """
    path = Path(base_output_path)
    stem = path.stem
    extension = path.suffix
    parent = path.parent
    
    new_filename = f"{stem}{suffix}{extension}"
    return parent / new_filename


def categorize_status(status):
    """
    Categorize an ena_status_2 string into the output file categories it belongs to.

    Returns a set containing one or more of:
    'ena_matches', 'errors', 'possible_homonym', 'no_ena_matches'

    TAXONOMIC_MISMATCH_HOMONYM belongs to both 'possible_homonym' and
    'no_ena_matches': ENA only holds the name in a different phylum, so the
    taxon has no ENA taxid and must continue to the GBIF round 2 search.
    """
    if not status or str(status).strip() == '':
        return {'errors'}

    if status == 'MATCH_TAXONOMICALLY_CONSISTENT':
        return {'ena_matches'}
    elif status == 'MAN_VER_NAME_EMPTY' or str(status).startswith('API_ERROR'):
        return {'errors'}
    elif status == 'MATCH_PHYLUM_UNCHECKED':
        return {'possible_homonym'}
    elif status == 'TAXONOMIC_MISMATCH_HOMONYM':
        return {'possible_homonym', 'no_ena_matches'}
    elif status in ('NO_EXACT_MATCH', 'NO_RESULTS_RETURNED'):
        return {'no_ena_matches'}
    else:
        logger.warning(f"Unexpected ena_status_2 '{status}' - categorizing as error")
        return {'errors'}


def write_split_outputs(output_df, base_output_path, logger):
    """
    Split the output dataframe by ena_status_2 category and write to separate files.
    Only selected/processed rows are present in the dataframe at this point.

    Categories:
    - ena_matches: MATCH_TAXONOMICALLY_CONSISTENT
    - errors: MAN_VER_NAME_EMPTY, API_ERROR:*
    - possible_homonym: MATCH_PHYLUM_UNCHECKED, TAXONOMIC_MISMATCH_HOMONYM
    - no_ena_matches: NO_EXACT_MATCH, NO_RESULTS_RETURNED, TAXONOMIC_MISMATCH_HOMONYM
    - filtered: All records EXCEPT MATCH_TAXONOMICALLY_CONSISTENT

    Note: TAXONOMIC_MISMATCH_HOMONYM rows appear in both possible_homonym and
    no_ena_matches.
    """
    category_suffixes = {
        'ena_matches': '-ena_matches',
        'errors': '-errors',
        'possible_homonym': '-possible_homonym',
        'no_ena_matches': '-no_ena_matches'
    }

    categories = output_df['ena_status_2'].apply(categorize_status)

    for category, suffix in category_suffixes.items():
        category_df = output_df[categories.apply(lambda cats: category in cats)]
        output_path = get_output_filepath(base_output_path, suffix)
        category_df.to_csv(output_path, index=False)
        logger.info(f"Wrote {len(category_df)} rows to '{output_path}'")

    # Filtered file: everything except confirmed matches
    filtered_df = output_df[output_df['ena_status_2'] != 'MATCH_TAXONOMICALLY_CONSISTENT']
    filtered_output_path = get_output_filepath(base_output_path, '-filtered')
    filtered_df.to_csv(filtered_output_path, index=False)
    logger.info(
        f"Wrote {len(filtered_df)} rows to '{filtered_output_path}' "
        f"(filtered: excludes MATCH_TAXONOMICALLY_CONSISTENT rows)"
    )


def main(input_file, output_file, man_verify_col):
    setup_logging(output_file)
    
    logger.info(f"Reading input file: {input_file}")
    df = read_file(input_file)
    logger.info(f"Read {len(df)} rows from input file")
    logger.info(f"Input columns: {df.columns.tolist()}")

    # Check that the search column exists
    if man_verify_col not in df.columns:
        logger.error(f"Search column '{man_verify_col}' not found in input.")
        logger.error(f"Available columns: {df.columns.tolist()}")
        sys.exit(1)

    # Select rows meeting processing conditions
    logger.info("Applying row selection criteria...")
    flags_present = has_flag_columns(df)
    df_selected = select_rows(df, flags_present)

    if len(df_selected) == 0:
        logger.warning("No rows met the selection criteria. No output files will be written.")
        sys.exit(0)

    if flags_present:
        logger.info(f"ENA search column: '{man_verify_col}' (Condition 1) / "
                    f"'{man_verify_col}' or 'gbif_species' (Condition 2)")
    else:
        logger.info(f"ENA search column: '{man_verify_col}' (all rows)")
    logger.info(f"Total rows selected for processing: {len(df_selected)}")

    logger.info("Starting ENA taxonomy searches (rate limited to 25 queries/second)...")
    df_with_ena = process_dataframe(df_selected, man_verify_col, flags_present, logger)
    logger.info("Completed ENA taxonomy searches")
    
    # Save combined output (selected rows only, ENA columns appended)
    df_with_ena.to_csv(output_file, index=False)
    logger.info(f"Combined output written to '{output_file}' ({len(df_with_ena)} rows)")
    
    # Write split output files by category
    logger.info("Writing split output files by ena_status_2 category...")
    write_split_outputs(df_with_ena, output_file, logger)
    
    # Summary statistics
    status_counts = df_with_ena['ena_status_2'].value_counts()
    logger.info("Summary of ENA search results:")
    for status, count in status_counts.items():
        logger.info(f"  {status}: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Search ENA taxonomy database for selected rows in the input file. '
                    'If the 03_gbif_name_processor.py flag columns are present, rows are selected '
                    'if manual_verification_needed=yes, OR if name_to_use=gbif AND '
                    'check_ENA_with_GBIF=yes (and not manual verification); all other rows are '
                    'excluded. If the flag columns are absent, every row is searched using '
                    '--man_verify_col.'
    )
    parser.add_argument('--input', '-i', dest='input', required=True,
                        help='Input CSV/TSV/XLSX file path')
    parser.add_argument('--output', '-o', dest='output', required=True,
                        help='Output CSV file path')
    parser.add_argument('--man_verify_col', '-mv', dest='man_verify_col', default='confirmed_taxonomy',
                        help='Column name to use as ENA search term (default: confirmed_taxonomy)')
    
    args = parser.parse_args()
    main(args.input, args.output, args.man_verify_col)
