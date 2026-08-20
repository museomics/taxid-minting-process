#!/usr/bin/env python3
"""
GBIF Backbone Taxonomy Search Script

This script takes a CSV file containing scientific names and searches them against
the GBIF backbone taxonomy database using the pygbif API. Results are appended
to the input data and written to an output file.

The script implements a multi-step search strategy to maximise match rates:

1. Standard Search: Initial name_backbone() search with scientific name only

2. Disambiguation: If the result returns usageKey=1 (Animalia) or a match at
   KINGDOM/PHYLUM rank, retry with phylum/order/family to disambiguate homonyms
   (e.g., Cantharis exists in Cantharidae, Meloidae, and Trochidae)

3. Homonym Resolution: If ena_status_1 == 'TAXONOMIC_MISMATCH_HOMONYM', retry
   name_backbone() with phylum parameter to disambiguate between kingdoms
   (e.g., Solieria exists in both Arthropoda and Rhodophyta)

4. Fuzzy Alternatives: If still no match found, use name_backbone() with
   verbose=True to retrieve alternative matches. These alternatives are filtered
   by phylum (and optionally family) to catch misspellings while avoiding
   spurious matches (e.g., "Etone flava" -> "Eteone flava")

Usage:
    python 02_gbif_backbone_search.py --input input.csv --output output/results.csv

Required input columns:
    - ID: Unique identifier for each record
    - scientificName: The scientific name to search
    - ena_status_1: Status from ENA search (used to detect homonym issues)
    - phylum: Taxonomic phylum (used for homonym resolution and fuzzy search)
    - family: Taxonomic family (used for fuzzy search constraints)

Optional input columns (used for disambiguation):
    - order: Taxonomic order

Output:
    - Original input columns plus GBIF search results prefixed with 'gbif_'
    - gbif_notes: Indicates if match required homonym resolution, disambiguation,
      fuzzy lookup, or records failure reasons (api_error, no_match_found,
      disambiguation_failed, fuzzy_search_failed)

Author: Dan Parsons @NHMUK
"""

import sys
import os
import pandas as pd
from pygbif import species
import logging
import argparse
import time
from datetime import datetime


# GBIF fields to capture from backbone search Note: 'synonym' field removed as it's redundant with 'status' (which returns SYNONYM, ACCEPTED, etc.) and was not reliably populated by the API
GBIF_FIELDS = [
    'usageKey',
    'scientificName',
    'canonicalName',
    'rank',
    'status',
    'matchType',
    'confidence',
    'kingdom',
    'phylum',
    'class',
    'order',
    'family',
    'genus',
    'species',
    'kingdomKey',
    'phylumKey',
    'classKey',
    'orderKey',
    'familyKey',
    'genusKey',
    'speciesKey',
    'acceptedUsageKey'
]

# Ranks that are too broad to be useful
INVALID_RANKS = {'KINGDOM', 'PHYLUM'}


# === translate the v2 response into the flat v1 field set ========
#
# In v2 the classification is a list, one entry per rank, rather than named
# fields. Only the ranks in GBIF_FIELDS are kept - v2 can also return DOMAIN,
# SUBPHYLUM, SECTION_BOTANY and others, which are ignored.

CLASSIFICATION_RANKS = {
    'KINGDOM': 'kingdom',
    'PHYLUM': 'phylum',
    'CLASS': 'class',
    'ORDER': 'order',
    'FAMILY': 'family',
    'GENUS': 'genus',
    'SPECIES': 'species',
}


def normalise_v2(response):
    """
    Flatten a GBIF v2 species/match response into the flat GBIF_FIELDS set from v1.

    Needed because pygbif 0.6.6 switched to GBIF's v2 endpoint, which nests
    the fields (usage, classification, diagnostics) rather than returning
    them flat. Everything downstream still reads the v1 field names.

    Args:
        response: dict from species.name_backbone(), or one element of
                  response['diagnostics']['alternatives']

    Returns:
        dict: every key in GBIF_FIELDS, with 'NOT_FOUND' where absent
    """    
    
    flat = {field: 'NOT_FOUND' for field in GBIF_FIELDS}

    if not response:
        return flat

    # Not every response has a 'usage' section: a verbose search that finds nothing returns only 'diagnostics' and 'synonym'.	
    usage = response.get('usage')
    if not usage:
        usage = response if response.get('key') is not None else {}

# if a response ever carries its name fields at the top level, read them there rather than silently returning NOT_FOUND for everything.		
    accepted = response.get('acceptedUsage') or {}
    diagnostics = response.get('diagnostics') or {}

    # Match details sit under 'diagnostics' in a full response, but at the top level in an alternative entry.
    match_type = diagnostics.get('matchType', response.get('matchType'))
    confidence = diagnostics.get('confidence', response.get('confidence'))
    note = diagnostics.get('note', response.get('note'))

    if usage.get('key') is not None:
        flat['usageKey'] = usage['key']
    if usage.get('name') is not None:
        flat['scientificName'] = usage['name']
    if usage.get('canonicalName') is not None:
        flat['canonicalName'] = usage['canonicalName']
    if usage.get('rank') is not None:
        flat['rank'] = usage['rank']
    if usage.get('status') is not None:
        flat['status'] = usage['status']

    if accepted.get('key') is not None:
        flat['acceptedUsageKey'] = accepted['key']

    if match_type is not None:
        flat['matchType'] = match_type

    # 'is not None' so that a confidence of 0 is kept, not treated as missing.
    if confidence is not None:
        flat['confidence'] = confidence
    if note is not None and 'note' in flat:
        flat['note'] = note

    for entry in response.get('classification') or []:
        field = CLASSIFICATION_RANKS.get(str(entry.get('rank', '')).upper())
        if not field:
            continue
        if entry.get('name') is not None:
            flat[field] = entry['name']
        if entry.get('key') is not None:
            flat[f'{field}Key'] = entry['key']

    return flat

def setup_logging(output_dir, prefix):
    """Set up logging to both file and console."""
    os.makedirs(output_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = os.path.join(output_dir, f"{prefix}_{timestamp}.log")
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Remove any existing handlers
    while logger.hasHandlers():
        logger.removeHandler(logger.handlers[0])
    
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # File handler
    file_handler = logging.FileHandler(log_filename)
    file_handler.setFormatter(formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Add handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    logger.info(f"Log file: {log_filename}")
    
    return logger


def is_valid_gbif_result(result):
    """
    Check if a GBIF result is valid and specific enough.
    
    Returns False if:
    - No result or matchType is NONE
    - usageKey is missing or equals 1 (root Animalia)
    - Rank is KINGDOM or PHYLUM (too broad)
    """
    if not result:
        return False
    if result.get('matchType') == 'NONE':
        return False
    usage_key = result.get('usageKey')
    # V2 CHANGE 2: v2 returns keys as strings, so "1" == 1 is False. Compare as
    # text or the root Animalia check never fires.
    if not usage_key or str(usage_key) in ('NOT_FOUND', '1'):
        return False
    if result.get('rank') in INVALID_RANKS:
        return False
    return True


def search_gbif_backbone(scientific_name, logger, phylum=None, order=None, family=None, max_retries=3, retry_delay=2):
    """
    Search a scientific name against the GBIF backbone taxonomy.
    
    Args:
        scientific_name: The scientific name to search
        logger: Logger instance
        phylum: Optional phylum to constrain search (for homonym resolution)
        order: Optional order to constrain search
        family: Optional family to constrain search
        max_retries: Maximum number of retry attempts
        retry_delay: Delay in seconds between retries
        
    Returns:
        dict: GBIF search results or empty dict with NOT_FOUND values
              May include '_error' key if an error occurred
    """
    # Handle empty or invalid names
    if not scientific_name or pd.isna(scientific_name) or str(scientific_name).strip() == '':
        logger.warning(f"Empty or invalid scientific name provided")
        return {field: 'NOT_FOUND' for field in GBIF_FIELDS}
    
    scientific_name = str(scientific_name).strip()
    
    for attempt in range(max_retries):
        try:
            # Build constraint description for logging
            constraints = []
            if phylum and not pd.isna(phylum):
                constraints.append(f"phylum='{phylum}'")
            if order and not pd.isna(order):
                constraints.append(f"order='{order}'")
            if family and not pd.isna(family):
                constraints.append(f"family='{family}'")
            
            constraint_str = f" with {', '.join(constraints)}" if constraints else ""
            logger.info(f"Searching GBIF backbone for: '{scientific_name}'{constraint_str} (attempt {attempt + 1}/{max_retries})")
            
            # Build search parameters
            search_params = {'strict': False}
            if phylum and not pd.isna(phylum):
                search_params['phylum'] = str(phylum).strip()
            if order and not pd.isna(order):
                search_params['order'] = str(order).strip()
            if family and not pd.isna(family):
                search_params['family'] = str(family).strip()
            
            # Call GBIF backbone API
            # Try both parameter names for compatibility with different pygbif versions
            try:
                # Newer versions use 'scientificName'
                raw_result = species.name_backbone(scientificName=scientific_name, **search_params)
            except TypeError:
                # Older versions use 'name'
                raw_result = species.name_backbone(name=scientific_name, **search_params)

            # flatten the nested v2 response before reading fields
            result = normalise_v2(raw_result)

            # Check if we got a match
            if result.get('matchType') == 'NONE' or result.get('usageKey') == 'NOT_FOUND':
                logger.info(f"No match found for '{scientific_name}'")
                no_match_result = {field: 'NOT_FOUND' for field in GBIF_FIELDS}
                no_match_result['matchType'] = 'NONE'
                no_match_result['_no_match'] = True
                return no_match_result
            
            # Log successful match
            logger.info(f"Match found: {result.get('scientificName')} "
                       f"(matchType: {result.get('matchType')}, "
                       f"rank: {result.get('rank')}, "
                       f"confidence: {result.get('confidence')}, "
                       f"status: {result.get('status')})")

            return result
            
        except Exception as e:
            logger.warning(f"API request failed for '{scientific_name}': {str(e)}")
            
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Max retries exceeded for '{scientific_name}'")
                result = {field: 'NOT_FOUND' for field in GBIF_FIELDS}
                result['_error'] = 'api_error'
                return result
    
    # Should not reach here, but just in case
    return {field: 'NOT_FOUND' for field in GBIF_FIELDS}


def search_gbif_with_disambiguation(scientific_name, logger, phylum=None, order=None, family=None, max_retries=3, retry_delay=2):
    """
    Search GBIF with automatic disambiguation if initial result is too broad.
    
    First attempts a standard search. If the result is invalid (usageKey=1, or
    rank is KINGDOM/PHYLUM), retries with higher taxonomy constraints.
    
    Args:
        scientific_name: The scientific name to search
        logger: Logger instance
        phylum: Optional phylum for disambiguation
        order: Optional order for disambiguation
        family: Optional family for disambiguation
        max_retries: Maximum number of retry attempts
        retry_delay: Delay in seconds between retries
        
    Returns:
        tuple: (dict of GBIF results, str notes about disambiguation)
    """
    notes = ''
    
    # Step 1: Standard search without constraints
    result = search_gbif_backbone(scientific_name, logger, max_retries=max_retries, retry_delay=retry_delay)
    
    # Check for API error
    if result.get('_error') == 'api_error':
        return result, 'api_error'
    
    # Check if result is valid
    if is_valid_gbif_result(result):
        return result, notes
    
    # Track if initial search found no match
    initial_no_match = result.get('_no_match', False)
    
    # Step 2: Result invalid or too broad - attempt disambiguation
    has_taxonomy = any([
        phylum and not pd.isna(phylum),
        order and not pd.isna(order),
        family and not pd.isna(family)
    ])
    
    if has_taxonomy:
        # Log why we're disambiguating
        # compare the key as text
        if str(result.get('usageKey')) == '1':
            logger.info(f"Initial search returned usageKey=1 (Animalia) for '{scientific_name}', attempting disambiguation")
        elif result.get('rank') in INVALID_RANKS:
            logger.info(f"Initial search returned too broad rank ({result.get('rank')}) for '{scientific_name}', attempting disambiguation")
        else:
            logger.info(f"Initial search failed for '{scientific_name}', attempting disambiguation with higher taxonomy")
        
        # Retry with higher taxonomy
        disambig_result = search_gbif_backbone(
            scientific_name, logger,
            phylum=phylum, order=order, family=family,
            max_retries=max_retries, retry_delay=retry_delay
        )
        
        # Check for API error in disambiguation attempt
        if disambig_result.get('_error') == 'api_error':
            return disambig_result, 'api_error'
        
        if is_valid_gbif_result(disambig_result):
            # Build disambiguation note
            used_constraints = []
            if phylum and not pd.isna(phylum):
                used_constraints.append(f"phylum={phylum}")
            if order and not pd.isna(order):
                used_constraints.append(f"order={order}")
            if family and not pd.isna(family):
                used_constraints.append(f"family={family}")
            
            notes = f"disambiguated_with_{'+'.join(used_constraints)}"
            logger.info(f"Disambiguation successful for '{scientific_name}' using {', '.join(used_constraints)}")
            return disambig_result, notes
        else:
            logger.info(f"Disambiguation failed for '{scientific_name}'")
            notes = 'disambiguation_failed'
    elif initial_no_match:
        # No taxonomy available and no match found
        notes = 'no_match_found'
    
    # Return original result (even if invalid) so caller can see what was returned
    return result, notes


def search_gbif_fuzzy(scientific_name, logger, phylum=None, family=None, max_retries=3, retry_delay=2):
    """
    Search a scientific name using GBIF name_backbone with verbose=True to find fuzzy alternatives.
    
    This is used as a fallback when name_backbone fails, to catch misspellings.
    Uses the alternatives returned by verbose mode, filtered by phylum to improve relevance.
    
    Args:
        scientific_name: The scientific name to search
        logger: Logger instance
        phylum: Optional phylum to filter alternatives (recommended)
        family: Optional family to filter alternatives
        max_retries: Maximum number of retry attempts
        retry_delay: Delay in seconds between retries
        
    Returns:
        tuple: (dict of GBIF results, bool indicating if match was found, str error type if any)
    """
    # Handle empty or invalid names
    if not scientific_name or pd.isna(scientific_name) or str(scientific_name).strip() == '':
        logger.warning(f"Empty or invalid scientific name provided for fuzzy search")
        return {field: 'NOT_FOUND' for field in GBIF_FIELDS}, False, None
    
    scientific_name = str(scientific_name).strip()
    
    for attempt in range(max_retries):
        try:
            constraints = []
            if phylum:
                constraints.append(f"phylum='{phylum}'")
            if family:
                constraints.append(f"family='{family}'")
            constraint_str = f" filtering by {', '.join(constraints)}" if constraints else ""
            
            logger.info(f"Fuzzy search (verbose alternatives) for: '{scientific_name}'{constraint_str} (attempt {attempt + 1}/{max_retries})")
            
            # Build search parameters - include phylum to improve alternative ranking
            search_params = {
                'verbose': True
            }
            if phylum and not pd.isna(phylum):
                search_params['phylum'] = str(phylum).strip()
            
            # Call GBIF backbone API with verbose=True
            # Try both parameter names for compatibility with different pygbif versions
            try:
                raw_result = species.name_backbone(scientificName=scientific_name, **search_params)
            except TypeError:
                raw_result = species.name_backbone(name=scientific_name, **search_params)

            # in v2, the alternatives sit under 'diagnostics' rather than at the top level, and each is nested like a full response, so
            # flatten each one before filtering. Falls back to the v1 location so
            # this still works on an older pygbif.
            raw_alternatives = (raw_result.get('diagnostics') or {}).get('alternatives')
            if raw_alternatives is None:
                raw_alternatives = raw_result.get('alternatives', [])
            alternatives = [normalise_v2(alt) for alt in raw_alternatives]

            if not alternatives:
                logger.info(f"No fuzzy alternatives found for '{scientific_name}'")
                return {field: 'NOT_FOUND' for field in GBIF_FIELDS}, False, 'fuzzy_search_failed'
            
            # Filter alternatives by phylum if provided
            candidates = alternatives
            
            if phylum and not pd.isna(phylum):
                phylum_lower = str(phylum).strip().lower()
                candidates = [c for c in candidates if str(c.get('phylum', '')).lower() == phylum_lower]
            
            # Optionally filter by family for stricter matching
            if family and not pd.isna(family) and candidates:
                family_lower = str(family).strip().lower()
                family_filtered = [c for c in candidates if str(c.get('family', '')).lower() == family_lower]
                # Only use family filter if it still leaves candidates
                if family_filtered:
                    candidates = family_filtered
            
            if not candidates:
                logger.info(f"No fuzzy alternatives for '{scientific_name}' after applying taxonomic filters")
                return {field: 'NOT_FOUND' for field in GBIF_FIELDS}, False, 'fuzzy_search_failed'
            
            # Filter out results with invalid ranks
            candidates = [c for c in candidates if c.get('rank') not in INVALID_RANKS]
            
            if not candidates:
                logger.info(f"No fuzzy alternatives for '{scientific_name}' after filtering out broad ranks")
                return {field: 'NOT_FOUND' for field in GBIF_FIELDS}, False, 'fuzzy_search_failed'
            
            # Filter to only FUZZY matchType alternatives
            fuzzy_candidates = [c for c in candidates if c.get('matchType') == 'FUZZY']
            if fuzzy_candidates:
                candidates = fuzzy_candidates
            
            # Sort by confidence (highest first) and take the top candidate
            # Confidence may be the string 'NOT_FOUND' after flattening, so guard the sort key
            candidates.sort(
                key=lambda x: x.get('confidence') if isinstance(x.get('confidence'), (int, float)) else 0,
                reverse=True
            )
            extracted = dict(candidates[0])

            # Log successful match
            logger.info(f"Fuzzy alternative found: {extracted.get('scientificName')} "
                       f"(canonicalName: {extracted.get('canonicalName')}, "
                       f"matchType: {extracted.get('matchType')}, "
                       f"confidence: {extracted.get('confidence')}, "
                       f"phylum: {extracted.get('phylum')})")

            # Ensure matchType reflects this was from fuzzy alternatives
            if extracted.get('matchType') != 'FUZZY':
                extracted['matchType'] = 'FUZZY_ALTERNATIVE'
            
            return extracted, True, None
            
        except Exception as e:
            logger.warning(f"Fuzzy search failed for '{scientific_name}': {str(e)}")
            
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Max retries exceeded for fuzzy search of '{scientific_name}'")
                return {field: 'NOT_FOUND' for field in GBIF_FIELDS}, False, 'api_error'
    
    return {field: 'NOT_FOUND' for field in GBIF_FIELDS}, False, 'fuzzy_search_failed'


def process_csv(input_file, output_file, logger):
    """
    Process the input CSV file and search each scientific name against GBIF.
    
    Implements a multi-step search strategy:
    1. Standard name_backbone search with automatic disambiguation if result too broad
    2. If ena_status_1 == 'TAXONOMIC_MISMATCH_HOMONYM', ensure phylum is used
    3. If still no match, try fuzzy lookup with verbose alternatives
    
    Args:
        input_file: Path to input CSV file
        output_file: Path to output CSV file
        logger: Logger instance
    """
    # Read input file
    logger.info(f"Reading input file: {input_file}")
    
    try:
        df = pd.read_csv(input_file)
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        sys.exit(1)
    
    logger.info(f"Read {len(df)} rows from input file")
    logger.info(f"Columns: {df.columns.tolist()}")
    
    # Validate required columns (updated to use _1 suffix for ena_status)
    required_columns = ['ID', 'scientificName', 'ena_status_1', 'phylum', 'family']
    missing_columns = [col for col in required_columns if col not in df.columns]
    
    if missing_columns:
        logger.error(f"Missing required columns: {missing_columns}")
        sys.exit(1)
    
    # Check for optional columns
    has_order = 'order' in df.columns
    if has_order:
        logger.info("Optional 'order' column found - will use for disambiguation")
    else:
        logger.info("Optional 'order' column not found - disambiguation will use phylum and family only")
    
    # Initialize GBIF result columns with prefix
    # Create these as dtype=object. v2 returns confidence as an
    # integer, and pandas >= 2.2 infers a string dtype from the 'NOT_FOUND'
    # placeholder, then raises TypeError when an int is written into it.
    for field in GBIF_FIELDS:
        df[f'gbif_{field}'] = pd.Series(['NOT_FOUND'] * len(df), index=df.index, dtype=object)

    # Initialize notes column
    df['gbif_notes'] = ''
    
    # Process each row
    total_rows = len(df)
    matches_found = 0
    homonym_resolved = 0
    disambiguated = 0
    fuzzy_matched = 0
    api_errors = 0
    no_matches = 0
    
    logger.info(f"Processing {total_rows} records...")
    
    for idx, row in df.iterrows():
        record_id = row['ID']
        scientific_name = row['scientificName']
        ena_status = row.get('ena_status_1', '')  # Updated to use _1 suffix
        phylum = row.get('phylum', '')
        order = row.get('order', '') if has_order else ''
        family = row.get('family', '')
        
        logger.info(f"Processing record {idx + 1}/{total_rows}: ID={record_id}")
        
        gbif_result = None
        notes = ''
        
        # Step 1: Check if this is a homonym case - if so, search with phylum directly
        if ena_status == 'TAXONOMIC_MISMATCH_HOMONYM':
            logger.info(f"Homonym detected for '{scientific_name}', searching with phylum constraint")
            gbif_result = search_gbif_backbone(scientific_name, logger, phylum=phylum)
            
            # Check for API error
            if gbif_result.get('_error') == 'api_error':
                notes = 'api_error'
                api_errors += 1
            elif is_valid_gbif_result(gbif_result):
                notes = 'homonym_resolved_with_phylum'
                homonym_resolved += 1
                logger.info(f"Homonym resolved with phylum for '{scientific_name}'")
        
        # Step 2: Standard search with automatic disambiguation
        if gbif_result is None or (not is_valid_gbif_result(gbif_result) and notes != 'api_error'):
            if gbif_result is None:
                # Standard search with disambiguation support
                gbif_result, disambig_notes = search_gbif_with_disambiguation(
                    scientific_name, logger,
                    phylum=phylum, order=order, family=family
                )
                if disambig_notes == 'api_error':
                    notes = 'api_error'
                    api_errors += 1
                elif disambig_notes == 'disambiguation_failed':
                    notes = 'disambiguation_failed'
                elif disambig_notes == 'no_match_found':
                    notes = 'no_match_found'
                elif disambig_notes:
                    notes = disambig_notes
                    disambiguated += 1
        
        # Step 3: If still no valid match, try fuzzy lookup
        if not is_valid_gbif_result(gbif_result) and notes != 'api_error':
            logger.info(f"No valid backbone match for '{scientific_name}', attempting fuzzy lookup")
            fuzzy_result, found, fuzzy_error = search_gbif_fuzzy(scientific_name, logger, phylum=phylum, family=family)
            
            if fuzzy_error == 'api_error':
                if notes:
                    notes = f"{notes};api_error"
                else:
                    notes = 'api_error'
                api_errors += 1
            elif found and is_valid_gbif_result(fuzzy_result):
                gbif_result = fuzzy_result
                if notes and notes not in ('no_match_found', 'disambiguation_failed'):
                    notes = f"{notes};suggested_match_from_fuzzy_alternative"
                else:
                    notes = 'suggested_match_from_fuzzy_alternative'
                fuzzy_matched += 1
                logger.info(f"Fuzzy alternative found for '{scientific_name}'")
            else:
                # Fuzzy search failed to find a match
                if notes:
                    notes = f"{notes};fuzzy_search_failed"
                else:
                    notes = 'fuzzy_search_failed'
        
        # Clean up internal flags from result before storing
        if '_error' in gbif_result:
            del gbif_result['_error']
        if '_no_match' in gbif_result:
            del gbif_result['_no_match']
        
        # Update DataFrame with results
        for field in GBIF_FIELDS:
            df.at[idx, f'gbif_{field}'] = gbif_result[field]
        
        df.at[idx, 'gbif_notes'] = notes
        
        # Track matches
        if is_valid_gbif_result(gbif_result):
            matches_found += 1
        elif 'no_match' in notes or notes == 'no_match_found':
            no_matches += 1
        
        # Small delay to be nice to the API
        if idx < total_rows - 1:
            time.sleep(0.1)
    
    # Create output directory if it doesn't exist
    output_dir = os.path.dirname(output_file)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Created output directory: {output_dir}")
    
    # Write output file
    logger.info(f"Writing output file: {output_file}")
    df.to_csv(output_file, index=False)
    
    # Summary
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total records processed: {total_rows}")
    logger.info(f"Valid matches found: {matches_found}")
    logger.info(f"  - Standard matches: {matches_found - homonym_resolved - disambiguated - fuzzy_matched}")
    logger.info(f"  - Homonym resolved: {homonym_resolved}")
    logger.info(f"  - Disambiguated (too broad initial match): {disambiguated}")
    logger.info(f"  - Fuzzy matches: {fuzzy_matched}")
    logger.info(f"No valid matches: {total_rows - matches_found}")
    logger.info(f"  - API errors: {api_errors}")
    logger.info(f"Match rate: {(matches_found / total_rows * 100):.1f}%")
    logger.info(f"Output written to: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Search scientific names against the GBIF backbone taxonomy database."
    )
    parser.add_argument(
        '-i', '--input', '--in',
        dest='input_file',
        required=True,
        help="Path to input CSV file (must contain 'ID', 'scientificName', 'ena_status_1', 'phylum', 'family' columns)"
    )
    parser.add_argument(
        '-o', '--output', '--out',
        dest='output_file',
        required=True,
        help="Path to output CSV file"
    )
    
    args = parser.parse_args()
    
    # Determine output directory for logging
    output_dir = os.path.dirname(args.output_file)
    if not output_dir:
        output_dir = '.'
    
    # Set up logging
    logger = setup_logging(output_dir, 'gbif_backbone_search')
    
    logger.info("=" * 60)
    logger.info("GBIF Backbone Taxonomy Search")
    logger.info("=" * 60)
    logger.info(f"Input file: {args.input_file}")
    logger.info(f"Output file: {args.output_file}")
    
    # Process the CSV
    process_csv(args.input_file, args.output_file, logger)
    
    logger.info("Done!")


if __name__ == "__main__":
    main()