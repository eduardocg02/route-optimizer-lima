#!/usr/bin/env python3
"""
Sync Bsale clients to Google Sheet with initial geocoding.
Can be run as a standalone script or triggered via API.

Usage:
    python sync_clients.py              # Full sync
    python sync_clients.py --new-only   # Only add new clients
"""

import os
import re
import sys
import time
import argparse
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# Import sheets module
from sheets import (
    get_worksheet,
    add_clients,
    get_existing_bsale_ids,
    batch_update_client_details,
    SHEET_COLUMNS
)

# API Configuration
BSALE_ACCESS_TOKEN = os.getenv("BSALE_ACCESS_TOKEN")
BSALE_API_URL = "https://api.bsale.io/v1"
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")


def geocode_address(address: str, city: str = "", district: str = "") -> str:
    """
    Convert an address string to a Google Maps URL using Google Geocoding API.
    Returns empty string if geocoding fails.
    """
    if not GOOGLE_API_KEY or not address:
        return ""
    
    # Clean address: remove apartment/office info that confuses geocoding
    clean_address = address
    clean_address = re.sub(
        r',?\s*(Dpto\.?|Departamento|Oficina|Dpto/Oficina|Dept\.?|Int\.?|Piso|Torre)\s*[A-Za-z0-9\-]+',
        '',
        clean_address,
        flags=re.IGNORECASE
    )
    clean_address = clean_address.strip().rstrip(',').strip()
    
    if not clean_address:
        return ""
    
    # Build full address string with district for accuracy
    full_address = clean_address
    if district:
        full_address += f", {district}"
    if city:
        full_address += f", {city}"
    full_address += ", Peru"
    
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": full_address,
        "key": GOOGLE_API_KEY,
        "language": "es",
        "region": "pe"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") == "OK" and data.get("results"):
            location = data["results"][0]["geometry"]["location"]
            lat = location["lat"]
            lng = location["lng"]
            # Return a Google Maps URL with the coordinates
            return f"https://www.google.com/maps?q={lat},{lng}"
        
        print(f"  Geocoding failed for: {full_address} - Status: {data.get('status')}")
        return ""
    
    except requests.RequestException as e:
        print(f"  Geocoding error for: {full_address} - {e}")
        return ""


def fetch_all_bsale_clients() -> list[dict]:
    """
    Fetch all clients from Bsale API with pagination.
    Returns list of client dictionaries.
    """
    if not BSALE_ACCESS_TOKEN:
        print("Error: BSALE_ACCESS_TOKEN not configured")
        return []
    
    headers = {
        "access_token": BSALE_ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    
    clients = []
    offset = 0
    limit = 50
    
    # Get total count first
    try:
        count_response = requests.get(
            f"{BSALE_API_URL}/clients/count.json",
            headers=headers,
            timeout=10
        )
        count_response.raise_for_status()
        total_count = count_response.json().get("count", 0)
        print(f"Total Bsale clients: {total_count}")
    except requests.RequestException as e:
        print(f"Error getting client count: {e}")
        return []
    
    while True:
        try:
            response = requests.get(
                f"{BSALE_API_URL}/clients.json",
                headers=headers,
                params={"limit": limit, "offset": offset, "state": 0},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            items = data.get("items", [])
            if not items:
                break
            
            for client in items:
                clients.append({
                    "bsale_id": client.get("id"),
                    "firstName": client.get("firstName", ""),
                    "lastName": client.get("lastName", ""),
                    "company": client.get("company", ""),
                    "phone": client.get("phone", ""),
                    "address": client.get("address", ""),
                    "city": client.get("city", ""),
                    "district": client.get("district", ""),
                })
            
            offset += len(items)
            print(f"  Fetched {offset}/{total_count} clients...")
            
            if len(items) < limit:
                break
            
            # Rate limiting
            time.sleep(0.1)
            
        except requests.RequestException as e:
            print(f"Error fetching clients at offset {offset}: {e}")
            break
    
    return clients


def sync_clients_to_sheet(new_only: bool = False):
    """
    Main sync function.
    
    Args:
        new_only: If True, only add new clients (don't update existing)
    """
    print(f"\n{'='*60}")
    print(f"Bsale to Google Sheets Sync - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")
    
    # Check worksheet is accessible
    worksheet = get_worksheet()
    if not worksheet:
        print("Error: Could not access Google Sheet. Check credentials.")
        return False
    
    print("✓ Connected to Google Sheet")
    
    # Get existing IDs
    existing_ids = get_existing_bsale_ids()
    print(f"  Existing clients in sheet: {len(existing_ids)}")
    
    # Fetch Bsale clients
    print("\nFetching clients from Bsale API...")
    bsale_clients = fetch_all_bsale_clients()
    
    if not bsale_clients:
        print("No clients fetched from Bsale.")
        return False
    
    print(f"✓ Fetched {len(bsale_clients)} clients from Bsale")
    
    # Separate new and existing clients
    new_clients = [c for c in bsale_clients if str(c.get("bsale_id")) not in existing_ids]
    existing_clients = [c for c in bsale_clients if str(c.get("bsale_id")) in existing_ids]
    
    print(f"  New clients to add: {len(new_clients)}")
    print(f"  Existing clients to update: {len(existing_clients)}")
    
    # ============ UPDATE EXISTING CLIENTS ============
    # Update details (name, company, phone, address, district, city)
    # but NOT maps_link, lat, lng, verified - those are managed via verification workflow
    if existing_clients and not new_only:
        print("\nUpdating existing clients' details...")
        updated = batch_update_client_details(existing_clients)
        print(f"✓ Updated {updated} existing clients")
    
    # ============ ADD NEW CLIENTS ============
    if not new_clients:
        print("\nNo new clients to add.")
        if not existing_clients or new_only:
            print("Sheet is up to date.")
        return True
    
    # Geocode addresses for new clients only
    print("\nGeocoding addresses for new clients...")
    geocoded_count = 0
    
    for i, client in enumerate(new_clients):
        address = client.get("address", "")
        city = client.get("city", "")
        district = client.get("district", "")
        
        if address:
            maps_link = geocode_address(address, city, district)
            client["maps_link"] = maps_link
            if maps_link:
                geocoded_count += 1
        else:
            client["maps_link"] = ""
        
        # Progress indicator
        if (i + 1) % 10 == 0 or i == len(new_clients) - 1:
            print(f"  Geocoded {i + 1}/{len(new_clients)} ({geocoded_count} successful)")
        
        # Rate limiting for geocoding API
        time.sleep(0.05)
    
    print(f"✓ Geocoding complete: {geocoded_count}/{len(new_clients)} successful")
    
    # Add new clients to sheet
    print("\nAdding new clients to Google Sheet...")
    added = add_clients(new_clients)
    print(f"✓ Added {added} new clients to sheet")
    
    print(f"\n{'='*60}")
    print("Sync complete!")
    print(f"{'='*60}\n")
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Sync Bsale clients to Google Sheet"
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Only add new clients (default behavior)"
    )
    
    args = parser.parse_args()
    
    success = sync_clients_to_sheet(new_only=args.new_only)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

