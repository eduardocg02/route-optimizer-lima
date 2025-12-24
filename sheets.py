"""
Google Sheets integration module for MiuRuta client management.
Uses gspread with service account authentication.
"""

import os
import re
import json
from datetime import datetime
from typing import Optional
import gspread
import requests
from google.oauth2.service_account import Credentials

# Sheet column names
SHEET_COLUMNS = [
    "bsale_id",
    "name", 
    "company",
    "phone",
    "address",
    "clean_address",      # User-editable formatted address for WhatsApp messages
    "district",
    "verified_district",  # District extracted from verified maps link
    "city",
    "maps_link",
    "lat",
    "lng",
    "verified",
    "last_updated"
]

# Google Sheets API scopes
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]


def get_sheets_client() -> Optional[gspread.Client]:
    """
    Create and return a gspread client using service account credentials.
    Returns None if credentials are not configured.
    """
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    
    if not service_account_json:
        print("GOOGLE_SERVICE_ACCOUNT_JSON not configured")
        return None
    
    try:
        # Check if it's a file path or JSON string
        if os.path.isfile(service_account_json):
            credentials = Credentials.from_service_account_file(
                service_account_json, 
                scopes=SCOPES
            )
        else:
            # Assume it's a JSON string (for deployment platforms)
            service_account_info = json.loads(service_account_json)
            credentials = Credentials.from_service_account_info(
                service_account_info,
                scopes=SCOPES
            )
        
        return gspread.authorize(credentials)
    
    except Exception as e:
        print(f"Error creating sheets client: {e}")
        return None


def get_worksheet() -> Optional[gspread.Worksheet]:
    """
    Get the main worksheet from the configured Google Sheet.
    Creates headers if the sheet is empty.
    """
    client = get_sheets_client()
    if not client:
        return None
    
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "")
    if not sheet_id:
        print("GOOGLE_SHEETS_ID not configured")
        return None
    
    try:
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.sheet1  # Use first sheet
        
        # Check if headers exist, if not create them
        existing_headers = worksheet.row_values(1)
        if not existing_headers or existing_headers != SHEET_COLUMNS:
            if not existing_headers:
                worksheet.append_row(SHEET_COLUMNS)
            else:
                # Update headers if they don't match
                worksheet.update('A1:K1', [SHEET_COLUMNS])
        
        return worksheet
    
    except Exception as e:
        print(f"Error accessing worksheet: {e}")
        return None


def expand_short_url(url: str) -> str:
    """
    Expand a short Google Maps URL (goo.gl, maps.app) to full URL.
    Returns the original URL if expansion fails.
    """
    if not url:
        return url
    
    url = url.strip()
    
    # Check if it's a short link that needs expansion
    if "goo.gl" in url or "maps.app" in url:
        try:
            response = requests.head(url, allow_redirects=True, timeout=10)
            return response.url
        except requests.RequestException as e:
            print(f"Error expanding short URL: {e}")
            return url
    
    return url


def extract_coords_from_maps_link(url: str, expand: bool = False) -> tuple[Optional[float], Optional[float]]:
    """
    Extract latitude and longitude from a Google Maps URL.
    
    Args:
        url: The Google Maps URL
        expand: If True, expand short links before extracting coords
    """
    if not url:
        return None, None
    
    url = url.strip()
    
    # Expand short links if requested
    if expand:
        url = expand_short_url(url)
    
    # Pattern: !3d and !4d (actual place coordinates)
    place_pattern = r"!3d(-?\d+\.?\d*)!4d(-?\d+\.?\d*)"
    match = re.search(place_pattern, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern: @lat,lng (map center)
    at_pattern = r"@(-?\d+\.?\d*),(-?\d+\.?\d*)"
    match = re.search(at_pattern, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern: query params q=lat,lng
    query_pattern = r"[?&]q=(-?\d+\.?\d*),(-?\d+\.?\d*)"
    match = re.search(query_pattern, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    return None, None


def get_all_clients() -> list[dict]:
    """
    Fetch all clients from the Google Sheet.
    Returns a list of client dictionaries.
    """
    worksheet = get_worksheet()
    if not worksheet:
        return []
    
    try:
        # Get all records (excludes header row)
        records = worksheet.get_all_records()
        
        clients = []
        for record in records:
            # Convert to our expected format
            client = {
                "bsale_id": record.get("bsale_id", ""),
                "name": record.get("name", ""),
                "company": record.get("company", ""),
                "phone": record.get("phone", ""),
                "address": record.get("address", ""),
                "clean_address": record.get("clean_address", ""),
                "district": record.get("district", ""),
                "verified_district": record.get("verified_district", ""),
                "city": record.get("city", ""),
                "maps_link": record.get("maps_link", ""),
                "lat": record.get("lat", ""),
                "lng": record.get("lng", ""),
                "verified": record.get("verified", ""),
                "last_updated": record.get("last_updated", "")
            }
            
            # Parse lat/lng to float if present
            if client["lat"]:
                try:
                    client["lat"] = float(client["lat"])
                except (ValueError, TypeError):
                    client["lat"] = None
            else:
                client["lat"] = None
                
            if client["lng"]:
                try:
                    client["lng"] = float(client["lng"])
                except (ValueError, TypeError):
                    client["lng"] = None
            else:
                client["lng"] = None
            
            clients.append(client)
        
        return clients
    
    except Exception as e:
        print(f"Error fetching clients from sheet: {e}")
        return []


def get_client_by_bsale_id(bsale_id: int) -> Optional[dict]:
    """
    Find a specific client by their Bsale ID.
    """
    clients = get_all_clients()
    for client in clients:
        if str(client.get("bsale_id")) == str(bsale_id):
            return client
    return None


def find_client_row(worksheet: gspread.Worksheet, bsale_id: int) -> Optional[int]:
    """
    Find the row number for a client by Bsale ID.
    Returns None if not found.
    """
    try:
        # Find all cells in column A (bsale_id column)
        cell = worksheet.find(str(bsale_id), in_column=1)
        if cell:
            return cell.row
        return None
    except Exception:
        return None


def update_client(bsale_id: int, updates: dict) -> bool:
    """
    Update a client's data in the Google Sheet.
    
    Args:
        bsale_id: The Bsale client ID
        updates: Dictionary of fields to update (maps_link, verified, etc.)
    
    Returns:
        True if successful, False otherwise
    """
    worksheet = get_worksheet()
    if not worksheet:
        return False
    
    try:
        row_num = find_client_row(worksheet, bsale_id)
        if not row_num:
            print(f"Client {bsale_id} not found in sheet")
            return False
        
        # Map field names to column indices (1-based)
        column_map = {col: idx + 1 for idx, col in enumerate(SHEET_COLUMNS)}
        
        # If updating maps_link, also extract and update lat/lng
        if "maps_link" in updates:
            lat, lng = extract_coords_from_maps_link(updates["maps_link"])
            updates["lat"] = lat if lat else ""
            updates["lng"] = lng if lng else ""
        
        # Update each field
        for field, value in updates.items():
            if field in column_map:
                col_num = column_map[field]
                worksheet.update_cell(row_num, col_num, str(value) if value is not None else "")
        
        # Always update last_updated
        last_updated_col = column_map["last_updated"]
        worksheet.update_cell(row_num, last_updated_col, datetime.now().isoformat())
        
        return True
    
    except Exception as e:
        print(f"Error updating client {bsale_id}: {e}")
        return False


def add_clients(clients: list[dict]) -> int:
    """
    Add new clients to the Google Sheet.
    Skips clients that already exist (by bsale_id).
    
    Args:
        clients: List of client dictionaries with Bsale data
    
    Returns:
        Number of clients added
    """
    worksheet = get_worksheet()
    if not worksheet:
        return 0
    
    try:
        # Get existing bsale_ids
        existing_ids = set()
        try:
            existing_values = worksheet.col_values(1)[1:]  # Skip header
            existing_ids = {str(v) for v in existing_values if v}
        except Exception:
            pass
        
        added = 0
        rows_to_add = []
        
        for client in clients:
            bsale_id = str(client.get("bsale_id", ""))
            if bsale_id in existing_ids:
                continue
            
            # Prepare row data
            name = f"{client.get('firstName', '')} {client.get('lastName', '')}".strip()
            maps_link = client.get("maps_link", "")
            lat, lng = extract_coords_from_maps_link(maps_link)
            
            row = [
                bsale_id,                           # bsale_id
                name,                               # name
                client.get("company", ""),          # company
                client.get("phone", ""),            # phone
                client.get("address", ""),          # address
                "",                                 # clean_address (user editable)
                client.get("district", ""),         # district
                "",                                 # verified_district (from maps)
                client.get("city", ""),             # city
                maps_link,                          # maps_link
                str(lat) if lat else "",            # lat
                str(lng) if lng else "",            # lng
                "",                                 # verified (empty initially)
                datetime.now().isoformat()          # last_updated
            ]
            
            rows_to_add.append(row)
            added += 1
        
        # Batch append all new rows
        if rows_to_add:
            worksheet.append_rows(rows_to_add)
        
        return added
    
    except Exception as e:
        print(f"Error adding clients to sheet: {e}")
        return 0


def verify_client(bsale_id: int, clean_address: str = None, verified_district: str = None) -> bool:
    """
    Mark a client's address as verified.
    Optionally update clean_address and verified_district.
    """
    updates = {"verified": "yes"}
    
    if clean_address is not None:
        updates["clean_address"] = clean_address
    
    if verified_district is not None:
        updates["verified_district"] = verified_district
    
    return update_client(bsale_id, updates)


def fix_client_address(bsale_id: int, new_maps_link: str, clean_address: str = None, verified_district: str = None) -> bool:
    """
    Update a client's Google Maps link and mark as verified.
    Expands short links and extracts coordinates automatically.
    
    Args:
        bsale_id: The Bsale client ID
        new_maps_link: Google Maps URL
        clean_address: Optional user-formatted address for display
        verified_district: Optional district name extracted from maps
    """
    # Expand short link if needed (e.g., maps.app.goo.gl/abc123)
    expanded_url = expand_short_url(new_maps_link)
    
    # Extract coordinates from the expanded URL
    lat, lng = extract_coords_from_maps_link(expanded_url)
    
    # Store the expanded URL (or original if expansion failed) with coords
    updates = {
        "maps_link": expanded_url,
        "verified": "yes"
    }
    
    # Add coordinates if we extracted them
    if lat is not None and lng is not None:
        updates["lat"] = lat
        updates["lng"] = lng
    
    # Add clean address if provided
    if clean_address is not None:
        updates["clean_address"] = clean_address
    
    # Add verified district if provided
    if verified_district is not None:
        updates["verified_district"] = verified_district
    
    return update_client(bsale_id, updates)


def get_existing_bsale_ids() -> set[str]:
    """
    Get all Bsale IDs currently in the sheet.
    """
    worksheet = get_worksheet()
    if not worksheet:
        return set()
    
    try:
        values = worksheet.col_values(1)[1:]  # Skip header
        return {str(v) for v in values if v}
    except Exception as e:
        print(f"Error getting existing Bsale IDs: {e}")
        return set()


def update_client_details(bsale_id: int, details: dict) -> bool:
    """
    Update a client's basic details (name, company, phone, address, district, city).
    Does NOT update: maps_link, lat, lng, verified, last_updated.
    These fields are managed separately via verification workflow.
    
    Args:
        bsale_id: The Bsale client ID
        details: Dictionary with fields to update
    
    Returns:
        True if successful, False otherwise
    """
    worksheet = get_worksheet()
    if not worksheet:
        return False
    
    try:
        row_num = find_client_row(worksheet, bsale_id)
        if not row_num:
            return False
        
        # Only allow updating these fields (not maps_link, lat, lng, verified, last_updated)
        allowed_fields = ["name", "company", "phone", "address", "district", "city"]
        
        # Map field names to column indices (1-based)
        column_map = {col: idx + 1 for idx, col in enumerate(SHEET_COLUMNS)}
        
        updates_made = False
        for field, value in details.items():
            if field in allowed_fields and field in column_map:
                col_num = column_map[field]
                worksheet.update_cell(row_num, col_num, str(value) if value is not None else "")
                updates_made = True
        
        return updates_made
    
    except Exception as e:
        print(f"Error updating client details {bsale_id}: {e}")
        return False


def batch_update_client_details(clients: list[dict]) -> int:
    """
    Batch update multiple clients' details.
    Only updates: name, company, phone, address, district, city.
    Compares with existing data first to avoid unnecessary writes.
    
    Args:
        clients: List of client dicts with bsale_id and fields to update
    
    Returns:
        Number of clients updated
    """
    worksheet = get_worksheet()
    if not worksheet:
        return 0
    
    column_map = {col: idx + 1 for idx, col in enumerate(SHEET_COLUMNS)}
    
    try:
        # Get all existing data to compare (1 read request)
        print("  Fetching existing data for comparison...")
        all_records = worksheet.get_all_records()
        existing_map = {}
        for idx, record in enumerate(all_records):
            bsale_id = str(record.get("bsale_id", ""))
            if bsale_id:
                existing_map[bsale_id] = {
                    "row": idx + 2,  # +2 because row 1 is header, enumerate is 0-based
                    "data": record
                }
        
        updated = 0
        cells_to_update = []
        
        for client in clients:
            bsale_id = str(client.get("bsale_id", ""))
            if bsale_id not in existing_map:
                continue
            
            existing = existing_map[bsale_id]
            row_num = existing["row"]
            existing_data = existing["data"]
            
            # Build name from firstName/lastName
            new_name = f"{client.get('firstName', '')} {client.get('lastName', '')}".strip()
            
            # Check each field for changes
            field_values = {
                "name": new_name,
                "company": client.get("company", ""),
                "phone": client.get("phone", ""),
                "address": client.get("address", ""),
                "district": client.get("district", ""),
                "city": client.get("city", "")
            }
            
            client_needs_update = False
            for field, new_value in field_values.items():
                old_value = str(existing_data.get(field, ""))
                if str(new_value) != old_value:
                    client_needs_update = True
                    col_num = column_map[field]
                    cells_to_update.append({
                        "range": f"{chr(64 + col_num)}{row_num}",
                        "values": [[str(new_value)]]
                    })
            
            if client_needs_update:
                updated += 1
        
        print(f"  Found {updated} clients with changes ({len(cells_to_update)} cells to update)")
        
        # If nothing changed, skip the write entirely
        if not cells_to_update:
            return 0
        
        # Use gspread's batch_update for a single API call
        # Format: list of dicts with 'range' and 'values' keys
        if cells_to_update:
            worksheet.batch_update(cells_to_update)
        
        return updated
    
    except Exception as e:
        print(f"Error batch updating clients: {e}")
        return 0

