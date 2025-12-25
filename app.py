#!/usr/bin/env python3
"""
Route Optimizer Web Interface
A Flask app for optimizing delivery routes in Lima, Peru.
"""

import functools
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, Response

load_dotenv()

app = Flask(__name__)

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Bsale API configuration
BSALE_ACCESS_TOKEN = os.getenv("BSALE_ACCESS_TOKEN")
BSALE_API_URL = "https://api.bsale.io/v1"

# Basic Auth credentials from environment
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "admin")

# Client cache file path
CLIENTS_CACHE_FILE = Path(__file__).parent / "clients_cache.json"

# In-memory cache state
CLIENTS_CACHE = {
    "clients": [],
    "loaded": False,
    "loading": False,
    "loading_progress": 0,
    "total_count": 0,
    "last_updated": None
}

# Sync progress tracking for Google Sheets sync
SYNC_STATE = {
    "syncing": False,
    "stage": "",  # "fetching_bsale", "comparing", "updating", "done", "error"
    "progress": 0,
    "total": 0,
    "message": "",
    "new_clients": 0,
    "updated_clients": 0,
    "error": None
}


def check_auth(username, password):
    """Check if username/password combination is valid."""
    return username == AUTH_USERNAME and password == AUTH_PASSWORD


def authenticate():
    """Send a 401 response that enables basic auth."""
    return Response(
        'Acceso denegado. Por favor ingresa tus credenciales.',
        401,
        {'WWW-Authenticate': 'Basic realm="Route Optimizer"'}
    )


def requires_auth(f):
    """Decorator that requires HTTP Basic Auth."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


def extract_coords_from_url(url: str) -> tuple[float, float] | None:
    """Extract latitude and longitude from various Google Maps URL formats."""
    url = url.strip()
    
    if "goo.gl" in url or "maps.app" in url:
        try:
            response = requests.head(url, allow_redirects=True, timeout=10)
            url = response.url
        except requests.RequestException:
            return None
    
    # Pattern 1: !3d and !4d format (actual place coordinates in data parameter)
    place_lat = re.search(r'!3d(-?\d+\.?\d*)', url)
    place_lng = re.search(r'!4d(-?\d+\.?\d*)', url)
    if place_lat and place_lng:
        return float(place_lat.group(1)), float(place_lng.group(1))
    
    # Pattern 2: Coordinates in @ format (map view center - fallback)
    at_pattern = r"@(-?\d+\.?\d*),(-?\d+\.?\d*)"
    match = re.search(at_pattern, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern 3: Coordinates in query parameter
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    
    if "q" in query_params:
        q_value = query_params["q"][0]
        coord_pattern = r"(-?\d+\.?\d*),\s*(-?\d+\.?\d*)"
        match = re.search(coord_pattern, q_value)
        if match:
            return float(match.group(1)), float(match.group(2))
    
    # Pattern 4: Coordinates in the path
    path_pattern = r"/(-?\d+\.?\d*),(-?\d+\.?\d*)"
    match = re.search(path_pattern, parsed.path)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern 5: ll parameter
    if "ll" in query_params:
        ll_value = query_params["ll"][0]
        parts = ll_value.split(",")
        if len(parts) == 2:
            return float(parts[0]), float(parts[1])
    
    return None


def optimize_route(origin, destination, waypoints):
    """Use Google Routes API to compute the optimal route order."""
    if not GOOGLE_API_KEY:
        return {"error": "API key not configured"}
    
    def make_waypoint(coords):
        return {
            "location": {
                "latLng": {
                    "latitude": coords[0],
                    "longitude": coords[1]
                }
            }
        }
    
    request_body = {
        "origin": make_waypoint(origin),
        "destination": make_waypoint(destination),
        "intermediates": [make_waypoint(wp) for wp in waypoints],
        "travelMode": "DRIVE",
        "optimizeWaypointOrder": True,
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "es",
        "units": "METRIC",
    }
    
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "routes.optimizedIntermediateWaypointIndex,routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters",
    }
    
    try:
        response = requests.post(ROUTES_API_URL, json=request_body, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def generate_google_maps_url(origin, destination, ordered_waypoints):
    """Generate a Google Maps directions URL."""
    base_url = "https://www.google.com/maps/dir/"
    points = [origin] + ordered_waypoints + [destination]
    path_parts = [f"{lat},{lng}" for lat, lng in points]
    return base_url + "/".join(path_parts)


def generate_split_routes(origin, destination, ordered_waypoints, max_waypoints_per_route=8):
    """
    Split a route into multiple Google Maps URLs if there are too many waypoints.
    Google Maps supports max 10 points total (origin + waypoints + destination).
    We use 8 waypoints per route to leave room for origin/destination.
    
    Returns a list of route parts, each with:
    - url: Google Maps URL
    - start: Starting point coords
    - end: Ending point coords  
    - waypoints: List of waypoint coords for this part
    - part_number: 1-indexed part number
    - total_parts: Total number of parts
    """
    total_waypoints = len(ordered_waypoints)
    
    # If 8 or fewer waypoints, single route is fine
    if total_waypoints <= max_waypoints_per_route:
        return [{
            "url": generate_google_maps_url(origin, destination, ordered_waypoints),
            "start": origin,
            "end": destination,
            "waypoints": ordered_waypoints,
            "part_number": 1,
            "total_parts": 1
        }]
    
    # Split into multiple routes
    routes = []
    remaining_waypoints = ordered_waypoints.copy()
    current_start = origin
    part_number = 1
    
    # Calculate total parts needed
    total_parts = (total_waypoints + max_waypoints_per_route - 1) // max_waypoints_per_route
    
    while remaining_waypoints:
        # Take up to max_waypoints_per_route waypoints
        chunk = remaining_waypoints[:max_waypoints_per_route]
        remaining_waypoints = remaining_waypoints[max_waypoints_per_route:]
        
        # Determine the end point for this chunk
        if remaining_waypoints:
            # Not the last chunk - end at the last waypoint of this chunk
            # The next route will start from here
            chunk_end = chunk[-1]
            chunk_waypoints = chunk[:-1]  # All except the last one (which is the destination)
        else:
            # Last chunk - end at final destination
            chunk_end = destination
            chunk_waypoints = chunk
        
        routes.append({
            "url": generate_google_maps_url(current_start, chunk_end, chunk_waypoints),
            "start": current_start,
            "end": chunk_end,
            "waypoints": chunk_waypoints,
            "part_number": part_number,
            "total_parts": total_parts
        })
        
        # Next route starts where this one ended
        current_start = chunk_end
        part_number += 1
    
    return routes


def reverse_geocode(coords: tuple[float, float]) -> str:
    """Convert coordinates to a readable address using Google Geocoding API."""
    if not GOOGLE_API_KEY:
        return f"{coords[0]:.6f}, {coords[1]:.6f}"
    
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "latlng": f"{coords[0]},{coords[1]}",
        "key": GOOGLE_API_KEY,
        "language": "es",
        "result_type": "street_address|route|neighborhood|locality"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0].get("formatted_address", f"{coords[0]:.6f}, {coords[1]:.6f}")
        return f"{coords[0]:.6f}, {coords[1]:.6f}"
    except requests.RequestException:
        return f"{coords[0]:.6f}, {coords[1]:.6f}"


def format_duration(seconds):
    """Format seconds into human-readable duration."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"


def format_distance(meters):
    """Format meters into human-readable distance."""
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{meters} m"


def geocode_address(address: str, city: str = "", district: str = "") -> tuple[float, float] | None:
    """Convert an address string to coordinates using Google Geocoding API."""
    if not GOOGLE_API_KEY:
        return None
    
    # Clean address: remove apartment/office info that confuses geocoding
    # Common patterns: "Dpto 301", "Dpto/Oficina 301", "Oficina 502", "Dept. 101", "Int. 5"
    import re
    clean_address = address
    # Remove apartment/office patterns
    clean_address = re.sub(r',?\s*(Dpto\.?|Departamento|Oficina|Dpto/Oficina|Dept\.?|Int\.?|Piso|Torre)\s*[A-Za-z0-9\-]+', '', clean_address, flags=re.IGNORECASE)
    clean_address = clean_address.strip().rstrip(',').strip()
    
    # Build full address string with district for accuracy (e.g., "San Isidro", "Miraflores")
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
            return (location["lat"], location["lng"])
        return None
    except requests.RequestException:
        return None


def fetch_bsale_clients() -> list[dict]:
    """Get clients from in-memory cache."""
    return CLIENTS_CACHE["clients"]


def load_clients_from_file() -> list[dict]:
    """Load clients from local JSON cache file."""
    global CLIENTS_CACHE
    
    if not CLIENTS_CACHE_FILE.exists():
        return []
    
    try:
        with open(CLIENTS_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            clients = data.get("clients", [])
            last_updated = data.get("last_updated")
            CLIENTS_CACHE["clients"] = clients
            CLIENTS_CACHE["loaded"] = True
            CLIENTS_CACHE["last_updated"] = last_updated
            print(f"Loaded {len(clients)} clients from cache file (updated: {last_updated})")
            return clients
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading clients from file: {e}")
        return []


def save_clients_to_file(clients: list[dict]):
    """Save clients to local JSON cache file."""
    try:
        data = {
            "clients": clients,
            "last_updated": datetime.now().isoformat(),
            "count": len(clients)
        }
        with open(CLIENTS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(clients)} clients to cache file")
    except IOError as e:
        print(f"Error saving clients to file: {e}")


def fetch_bsale_clients_from_api() -> list[dict]:
    """Fetch clients from Bsale API and update cache."""
    global CLIENTS_CACHE
    
    if CLIENTS_CACHE["loading"]:
        print("Already loading clients, skipping...")
        return CLIENTS_CACHE["clients"]
    
    if not BSALE_ACCESS_TOKEN:
        print("BSALE_ACCESS_TOKEN not configured")
        return []
    
    CLIENTS_CACHE["loading"] = True
    CLIENTS_CACHE["loading_progress"] = 0
    
    clients = []
    offset = 0
    limit = 50
    
    headers = {
        "access_token": BSALE_ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    
    try:
        while True:
            response = requests.get(
                f"{BSALE_API_URL}/clients.json",
                headers=headers,
                params={"limit": limit, "offset": offset, "state": 0},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            total_count = data.get("count", 0)
            items = data.get("items", [])
            
            # Update progress
            CLIENTS_CACHE["total_count"] = total_count
            CLIENTS_CACHE["loading_progress"] = min(offset + len(items), total_count)
            
            if offset == 0:
                print(f"Bsale API: Total clients to fetch: {total_count}")
            
            if not items:
                break
            
            for client in items:
                clients.append({
                    "id": client.get("id"),
                    "firstName": client.get("firstName", ""),
                    "lastName": client.get("lastName", ""),
                    "company": client.get("company", ""),
                    "address": client.get("address", ""),
                    "city": client.get("city", ""),
                    "district": client.get("district", ""),  # District for better geocoding
                    "code": client.get("code", "")
                })
            
            offset += limit
            if offset >= total_count:
                break
        
        print(f"Total Bsale clients fetched: {len(clients)}")
        
        # Update in-memory cache
        CLIENTS_CACHE["clients"] = clients
        CLIENTS_CACHE["loaded"] = True
        CLIENTS_CACHE["loading_progress"] = total_count
        CLIENTS_CACHE["last_updated"] = datetime.now().isoformat()
        
        # Save to file
        save_clients_to_file(clients)
        
        return clients
    except requests.RequestException as e:
        print(f"Error fetching Bsale clients: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response content: {e.response.text}")
        return CLIENTS_CACHE["clients"]  # Return existing cached clients on error
    finally:
        CLIENTS_CACHE["loading"] = False


def preload_clients():
    """
    Load clients for the fallback Bsale cache system.
    
    Data Source Priority:
    1. Google Sheets (if configured) - Source of truth with verified addresses
    2. Bsale JSON cache (clients_cache.json) - Fallback when Sheets not available
    
    This function handles the Bsale cache fallback.
    """
    global CLIENTS_CACHE
    
    # Check if Google Sheets is available first
    try:
        from sheets import get_all_clients
        sheets_clients = get_all_clients()
        if sheets_clients:
            print(f"Google Sheets configured with {len(sheets_clients)} clients - using as primary source")
            # Don't need to load Bsale cache if Sheets is working
            return
    except Exception as e:
        print(f"Google Sheets not available: {e} - using Bsale cache fallback")
    
    # Fall back to Bsale cache
    # First, load from local file (instant)
    cached_clients = load_clients_from_file()
    
    if cached_clients:
        print(f"Using {len(cached_clients)} cached clients, refreshing in background...")
        CLIENTS_CACHE["loaded"] = True
    
    # Then refresh from API in background
    print("Starting background refresh from Bsale API...")
    thread = threading.Thread(target=fetch_bsale_clients_from_api)
    thread.daemon = True
    thread.start()


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MiuRuta - Optimizador de Rutas</title>
    <link rel="icon" type="image/png" href="/static/miushop-logo.png">
    <link rel="apple-touch-icon" href="/static/miushop-logo.png">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            /* Adoptamiu Pink Theme 🐱 */
            --bg-base: #fff5f7;
            --bg-surface: #ffffff;
            --bg-elevated: #fef1f3;
            --bg-hover: #fde4e8;
            --accent-primary: #e91e63;
            --accent-secondary: #f48fb1;
            --accent-success: #4caf50;
            --accent-warning: #ff9800;
            --accent-error: #f44336;
            --text-primary: #2d2d2d;
            --text-secondary: #666666;
            --text-muted: #999999;
            --border-subtle: rgba(233, 30, 99, 0.1);
            --border-default: rgba(233, 30, 99, 0.25);
            --glow-primary: rgba(233, 30, 99, 0.12);
            --glow-secondary: rgba(244, 143, 177, 0.15);
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        html {
            scroll-behavior: smooth;
        }
        
        body {
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-base);
            color: var(--text-primary);
            min-height: 100vh;
            overflow-x: hidden;
        }
        
        /* Soft pink background with subtle pattern */
        .bg-pattern {
            position: fixed;
            inset: 0;
            z-index: -1;
            background: 
                radial-gradient(ellipse 100% 60% at 50% -10%, rgba(244, 143, 177, 0.25), transparent),
                radial-gradient(ellipse 80% 50% at 100% 100%, rgba(233, 30, 99, 0.08), transparent),
                radial-gradient(ellipse 60% 40% at 0% 80%, rgba(244, 143, 177, 0.12), transparent);
        }
        
        .grid-overlay {
            position: fixed;
            inset: 0;
            z-index: -1;
            /* Subtle paw print pattern overlay */
            opacity: 0.03;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60' viewBox='0 0 60 60'%3E%3Cpath fill='%23e91e63' d='M30 25c-2.5 0-4.5 2-4.5 4.5s2 4.5 4.5 4.5 4.5-2 4.5-4.5-2-4.5-4.5-4.5zm-8-4c-1.5 0-3 1.5-3 3s1.5 3 3 3 3-1.5 3-3-1.5-3-3-3zm16 0c-1.5 0-3 1.5-3 3s1.5 3 3 3 3-1.5 3-3-1.5-3-3-3zm-12 10c-1.5 0-3 1.5-3 3s1.5 3 3 3 3-1.5 3-3-1.5-3-3-3zm8 0c-1.5 0-3 1.5-3 3s1.5 3 3 3 3-1.5 3-3-1.5-3-3-3z'/%3E%3C/svg%3E");
            background-size: 60px 60px;
        }
        
        /* Layout */
        .app-container {
            max-width: 1000px;
            margin: 0 auto;
            padding: 32px 24px 60px;
        }
        
        /* Header */
        .app-header {
            text-align: center;
            margin-bottom: 48px;
            padding-top: 20px;
        }
        
        .logo {
            display: inline-flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 16px;
        }
        
        .logo-icon {
            width: 56px;
            height: 56px;
            background: linear-gradient(135deg, #f8bbd9, #f48fb1);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            box-shadow: 0 4px 20px rgba(233, 30, 99, 0.2);
            border: 3px solid white;
        }
        
        .logo-text {
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 2.2rem;
            font-weight: 800;
            background: linear-gradient(135deg, #e91e63, #f48fb1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .tagline {
            color: var(--text-secondary);
            font-size: 1.1rem;
            font-weight: 400;
            max-width: 500px;
            margin: 0 auto;
            line-height: 1.6;
        }
        
        /* Cards */
        .card {
            background: var(--bg-surface);
            border: 2px solid var(--border-subtle);
            border-radius: 24px;
            padding: 28px;
            margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(233, 30, 99, 0.06);
            transition: all 0.3s ease;
        }
        
        .card:hover {
            border-color: var(--border-default);
            box-shadow: 0 8px 30px rgba(233, 30, 99, 0.1);
        }
        
        .card-bsale {
            position: relative;
            z-index: 100;
        }
        
        .card-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 24px;
        }
        
        .card-icon {
            width: 44px;
            height: 44px;
            background: linear-gradient(135deg, #fce4ec, #f8bbd9);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
        }
        
        .card-title {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: var(--text-secondary);
            flex: 1;
        }
        
        .refresh-btn {
            background: rgba(244, 143, 177, 0.2);
            border: 1px solid rgba(233, 30, 99, 0.25);
            border-radius: 50%;
            padding: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            font-size: 0.9rem;
            line-height: 1;
            width: 36px;
            height: 36px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .refresh-btn:hover {
            background: rgba(244, 143, 177, 0.35);
            border-color: var(--accent-primary);
        }
        
        .refresh-btn.loading .refresh-icon {
            display: inline-block;
            animation: spin 1s linear infinite;
        }
        
        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        
        .cache-status {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        
        .cache-status .status-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--accent-success);
        }
        
        .cache-status.loading .status-dot {
            background: var(--accent-warning);
            animation: pulse 1s ease-in-out infinite;
        }
        
        .cache-status.syncing .status-dot {
            background: var(--accent-primary);
            animation: pulse 1s ease-in-out infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }
        
        /* Sync progress bar */
        .sync-progress-container {
            width: 100%;
            margin-top: 8px;
            display: none;
        }
        
        .sync-progress-container.active {
            display: block;
        }
        
        .sync-progress-bar {
            width: 100%;
            height: 6px;
            background: rgba(0, 0, 0, 0.05);
            border-radius: 3px;
            overflow: hidden;
        }
        
        .sync-progress-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--accent-primary), var(--accent-secondary));
            border-radius: 3px;
            transition: width 0.3s ease;
            width: 0%;
        }
        
        .sync-progress-text {
            font-size: 0.7rem;
            color: var(--text-muted);
            margin-top: 4px;
            text-align: center;
        }
        
        /* Form elements */
        .input-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }
        
        @media (max-width: 640px) {
            .input-row {
                grid-template-columns: 1fr;
            }
        }
        
        .input-group {
            margin-bottom: 20px;
        }
        
        .input-group:last-child {
            margin-bottom: 0;
        }
        
        .input-label {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 10px;
        }
        
        .input-label-icon {
            font-size: 14px;
        }
        
        .input-field {
            width: 100%;
            padding: 16px 18px;
            background: #fef7f9;
            border: 2px solid var(--border-subtle);
            border-radius: 16px;
            color: var(--text-primary);
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 0.9rem;
            transition: all 0.2s ease;
        }
        
        .input-field::placeholder {
            color: var(--text-muted);
        }
        
        .input-field:hover {
            border-color: var(--border-default);
        }
        
        .input-field:focus {
            outline: none;
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 4px var(--glow-primary);
        }
        
        textarea.input-field {
            min-height: 160px;
            resize: vertical;
            line-height: 1.7;
        }
        
        .input-hint {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 8px;
        }
        
        .input-prefilled {
            border-color: rgba(233, 30, 99, 0.3);
            background: rgba(233, 30, 99, 0.05);
        }
        
        .prefilled-label {
            display: inline-block;
            margin-top: 8px;
            padding: 6px 12px;
            background: rgba(233, 30, 99, 0.1);
            border: 1px solid rgba(233, 30, 99, 0.25);
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--accent-primary);
        }
        
        .quick-fill-btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            margin-top: 8px;
            padding: 8px 14px;
            background: rgba(244, 143, 177, 0.2);
            border: 1px solid rgba(233, 30, 99, 0.25);
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--accent-primary);
            cursor: pointer;
            transition: all 0.2s ease;
            font-family: inherit;
        }
        
        .quick-fill-btn:hover {
            background: rgba(244, 143, 177, 0.35);
            border-color: var(--accent-primary);
        }
        
        .quick-fill-btn.active {
            background: rgba(233, 30, 99, 0.15);
            border-color: rgba(233, 30, 99, 0.4);
            color: var(--accent-primary);
        }
        
        /* Buttons */
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 18px 32px;
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 1rem;
            font-weight: 600;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            transition: all 0.25s ease;
            text-decoration: none;
        }
        
        .btn-primary {
            width: 100%;
            background: linear-gradient(135deg, #e91e63, #f48fb1);
            color: white;
            box-shadow: 0 4px 20px rgba(233, 30, 99, 0.3);
        }
        
        .btn-primary:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 30px rgba(233, 30, 99, 0.4);
        }
        
        .btn-primary:active {
            transform: translateY(-1px);
        }
        
        .btn-primary:disabled {
            background: linear-gradient(135deg, #ccc, #ddd);
            color: #999;
            cursor: not-allowed;
            box-shadow: none;
            transform: none;
        }
        
        .btn-primary:disabled:hover {
            transform: none;
            box-shadow: none;
        }
        
        .unverified-warning {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            margin-top: 12px;
            padding: 10px 16px;
            background: rgba(255, 152, 0, 0.1);
            border: 1px solid rgba(255, 152, 0, 0.3);
            border-radius: 10px;
            color: #e65100;
            font-size: 0.85rem;
            text-align: center;
        }
        
        .btn-secondary {
            background: white;
            color: var(--accent-primary);
            border: 2px solid var(--border-default);
        }
        
        .btn-secondary:hover {
            background: var(--bg-hover);
            border-color: var(--border-default);
        }
        
        .btn-maps {
            background: linear-gradient(135deg, #4285f4, #34a853);
            color: white;
            box-shadow: 0 4px 20px rgba(66, 133, 244, 0.3);
        }
        
        .btn-maps:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 40px rgba(66, 133, 244, 0.4);
        }
        
        /* Route parts for multi-route */
        .route-parts-notice {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 16px;
            background: rgba(66, 133, 244, 0.1);
            border: 1px solid rgba(66, 133, 244, 0.2);
            border-radius: 12px;
            color: #1a73e8;
            font-size: 0.9rem;
            margin-bottom: 12px;
        }
        
        .route-parts-buttons {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            justify-content: center;
        }
        
        .route-part-btn {
            flex: 1;
            min-width: 140px;
            max-width: 200px;
        }
        
        /* Route Summary Section */
        .route-summary-section {
            margin-top: 24px;
            padding: 20px;
            background: linear-gradient(135deg, #fff5f8 0%, #ffeef3 100%);
            border-radius: 16px;
            border: 2px solid rgba(236, 72, 153, 0.15);
        }
        
        .route-summary-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }
        
        .route-summary-header h3 {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--primary-dark);
            margin: 0;
        }
        
        .route-summary-input-row {
            display: flex;
            gap: 12px;
            align-items: center;
            margin-bottom: 12px;
        }
        
        .route-summary-input-row label {
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-secondary);
            white-space: nowrap;
        }
        
        .route-summary-input-row input {
            flex: 1;
            padding: 10px 14px;
            border: 2px solid #e5e5e5;
            border-radius: 10px;
            font-size: 0.95rem;
            font-family: inherit;
            font-weight: 600;
            color: var(--primary-dark);
        }
        
        .route-summary-input-row input:focus {
            outline: none;
            border-color: var(--primary);
        }
        
        .route-summary-text {
            width: 100%;
            min-height: 280px;
            padding: 16px;
            border: 2px solid #e5e5e5;
            border-radius: 12px;
            font-family: 'SF Mono', 'Monaco', 'Inconsolata', monospace;
            font-size: 0.9rem;
            line-height: 1.6;
            resize: vertical;
            background: white;
            color: #333;
            box-sizing: border-box;
        }
        
        .route-summary-text:focus {
            outline: none;
            border-color: var(--primary);
        }
        
        .btn-copy {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 10px 16px;
            background: white;
            border: 2px solid var(--primary);
            color: var(--primary);
            border-radius: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .btn-copy:hover {
            background: var(--primary);
            color: white;
        }
        
        .btn-copy.copied {
            background: #10b981;
            border-color: #10b981;
            color: white;
        }
        
        /* Results */
        #results {
            display: none;
        }
        
        .results-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-wrap: wrap;
            gap: 24px;
            margin-bottom: 32px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--border-subtle);
        }
        
        .results-title {
            font-size: 1.5rem;
            font-weight: 700;
            color: var(--text-primary);
        }
        
        .results-subtitle {
            font-size: 0.9rem;
            color: var(--text-muted);
            margin-top: 4px;
        }
        
        .stats-grid {
            display: flex;
            gap: 32px;
        }
        
        .stat-item {
            text-align: right;
        }
        
        .stat-value {
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.75rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent-primary), var(--accent-success));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .stat-label {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            margin-top: 2px;
        }
        
        /* Timeline */
        .timeline {
            position: relative;
            padding-left: 36px;
        }
        
        .timeline::before {
            content: '';
            position: absolute;
            left: 11px;
            top: 24px;
            bottom: 24px;
            width: 2px;
            background: linear-gradient(180deg, 
                #e91e63 0%, 
                #f48fb1 50%, 
                #4caf50 100%);
            border-radius: 2px;
        }
        
        .timeline-item {
            position: relative;
            padding: 20px 0;
            border-bottom: 1px solid var(--border-subtle);
        }
        
        .timeline-item:last-child {
            border-bottom: none;
        }
        
        .timeline-marker {
            position: absolute;
            left: -36px;
            top: 24px;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: var(--bg-surface);
            border: 3px solid var(--accent-secondary);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 10px;
            font-weight: 700;
            color: var(--accent-secondary);
        }
        
        .timeline-item.start .timeline-marker {
            background: #e91e63;
            border-color: #e91e63;
            color: white;
            box-shadow: 0 0 15px rgba(233, 30, 99, 0.3);
        }
        
        .timeline-item.end .timeline-marker {
            background: #4caf50;
            border-color: #4caf50;
            color: white;
            box-shadow: 0 0 15px rgba(76, 175, 80, 0.3);
        }
        
        .timeline-content {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 16px;
        }
        
        .timeline-info {
            flex: 1;
        }
        
        .timeline-label {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            margin-bottom: 6px;
        }
        
        .timeline-item.start .timeline-label,
        .timeline-item.end .timeline-label {
            color: var(--accent-primary);
        }
        
        .timeline-address {
            font-size: 1rem;
            font-weight: 500;
            color: var(--text-primary);
            line-height: 1.5;
            margin-bottom: 4px;
        }
        
        .timeline-coords {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.7rem;
            color: var(--text-muted);
        }
        
        .timeline-metrics {
            text-align: right;
            flex-shrink: 0;
        }
        
        .metric-distance {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--accent-primary);
        }
        
        .metric-time {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-top: 2px;
        }
        
        /* Action buttons */
        .action-row {
            display: flex;
            gap: 12px;
            margin-top: 28px;
        }
        
        .action-row .btn {
            flex: 1;
        }
        
        @media (max-width: 500px) {
            .action-row {
                flex-direction: column;
            }
        }
        
        /* Loading */
        .loading-container {
            display: none;
            text-align: center;
            padding: 60px 20px;
        }
        
        .loading-container.active {
            display: block;
        }
        
        .loader {
            width: 56px;
            height: 56px;
            margin: 0 auto 20px;
            border-radius: 50%;
            border: 3px solid var(--border-subtle);
            border-top-color: var(--accent-primary);
            animation: spin 1s linear infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .loading-text {
            color: var(--text-secondary);
            font-size: 1rem;
        }
        
        /* Errors */
        .error-banner {
            background: rgba(248, 113, 113, 0.1);
            border: 1px solid rgba(248, 113, 113, 0.3);
            border-radius: 12px;
            padding: 16px 20px;
            color: var(--accent-error);
            font-size: 0.9rem;
            margin-top: 16px;
        }
        
        /* Bsale Client Selector */
        .client-selector {
            position: relative;
        }
        
        .client-search {
            width: 100%;
            padding: 16px 18px;
            padding-right: 45px;
            background: var(--bg-base);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            color: var(--text-primary);
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 0.95rem;
            transition: all 0.2s ease;
        }
        
        .client-search::placeholder {
            color: var(--text-muted);
        }
        
        .client-search:focus {
            outline: none;
            border-color: var(--accent-secondary);
            box-shadow: 0 0 0 4px var(--glow-secondary);
        }
        
        .client-dropdown {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            max-height: 320px;
            overflow-y: auto;
            background: white;
            border: 2px solid var(--border-default);
            border-radius: 16px;
            margin-top: 8px;
            z-index: 9999;
            display: none;
            box-shadow: 0 10px 40px rgba(233, 30, 99, 0.15);
        }
        
        .client-dropdown.active {
            display: block;
        }
        
        .client-option {
            padding: 14px 18px;
            cursor: pointer;
            border-bottom: 1px solid var(--border-subtle);
            transition: background 0.15s;
        }
        
        .client-option:last-child {
            border-bottom: none;
        }
        
        .client-option:hover,
        .client-option.highlighted {
            background: var(--bg-hover);
        }
        
        .client-option.highlighted {
            outline: 2px solid var(--accent-primary);
            outline-offset: -2px;
        }
        
        .client-option.selected {
            background: rgba(233, 30, 99, 0.1);
        }
        
        .client-name {
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 4px;
        }
        
        .client-address {
            font-size: 0.8rem;
            color: var(--text-secondary);
        }
        
        .client-code {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.7rem;
            color: var(--text-muted);
            margin-top: 2px;
        }
        
        .selected-clients {
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin-top: 12px;
        }
        
        .selected-clients:empty {
            display: none;
        }
        
        .client-tag {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 10px 14px;
            background: white;
            border: 1px solid var(--border-default);
            border-radius: 12px;
            font-size: 0.9rem;
            color: var(--text-primary);
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }
        
        .client-tag-info {
            display: flex;
            flex-direction: column;
            gap: 2px;
            flex: 1;
            min-width: 0;
        }
        
        .client-tag-name {
            font-weight: 500;
        }
        
        .client-tag-address {
            font-size: 0.8rem;
            color: var(--text-muted);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .client-tag-phone {
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        
        .client-tag-phone a {
            color: var(--text-secondary);
            text-decoration: none;
        }
        
        .client-tag-phone a:hover {
            color: var(--accent-primary);
            text-decoration: underline;
        }
        
        .client-tag-status {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }
        
        .client-tag-status.verified {
            background: #4caf50;
        }
        
        .client-tag-status.unverified {
            background: #ff9800;
        }
        
        .client-tag-actions {
            display: flex;
            align-items: center;
            gap: 2px;
            margin-left: 4px;
            padding-left: 8px;
            border-left: 1px solid var(--border-subtle);
        }
        
        .client-tag-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 32px;
            height: 32px;
            background: var(--bg-elevated);
            border: 1px solid var(--border-subtle);
            cursor: pointer;
            border-radius: 8px;
            font-size: 1rem;
            transition: all 0.15s;
        }
        
        .client-tag-btn:hover {
            background: var(--bg-hover);
            border-color: var(--border-default);
            transform: scale(1.05);
        }
        
        .client-tag-btn.maps-btn:hover {
            background: rgba(66, 133, 244, 0.1);
            border-color: rgba(66, 133, 244, 0.3);
        }
        
        .client-tag-btn.verify-btn {
            background: rgba(76, 175, 80, 0.1);
            border-color: rgba(76, 175, 80, 0.2);
            color: #2e7d32;
        }
        
        .client-tag-btn.verify-btn:hover {
            background: rgba(76, 175, 80, 0.2);
            border-color: rgba(76, 175, 80, 0.4);
        }
        
        .client-tag-btn.verify-btn.confirming {
            background: #4caf50;
            border-color: #4caf50;
            color: white;
            animation: pulse-green 0.8s infinite;
        }
        
        @keyframes pulse-green {
            0%, 100% { box-shadow: 0 0 0 0 rgba(76, 175, 80, 0.4); }
            50% { box-shadow: 0 0 0 6px rgba(76, 175, 80, 0); }
        }
        
        .client-tag-btn.fix-btn {
            background: rgba(255, 152, 0, 0.1);
            border-color: rgba(255, 152, 0, 0.2);
            color: #e65100;
        }
        
        .client-tag-btn.fix-btn:hover {
            background: rgba(255, 152, 0, 0.2);
            border-color: rgba(255, 152, 0, 0.4);
        }
        
        .client-tag-remove {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 24px;
            height: 24px;
            background: none;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            font-size: 1.2rem;
            line-height: 1;
            border-radius: 4px;
            opacity: 0.4;
            transition: all 0.15s;
            margin-left: 8px;
        }
        
        .client-tag-remove:hover {
            opacity: 1;
            color: var(--accent-error);
        }
        
        /* Verification status - green border for verified */
        .client-tag.verified {
            border-color: rgba(76, 175, 80, 0.4);
            border-left: 3px solid #4caf50;
        }
        
        .client-tag.unverified {
            border-color: rgba(255, 152, 0, 0.4);
            border-left: 3px solid #ff9800;
        }
        
        /* Compact verification badge for dropdown */
        .verification-badge {
            font-size: 0.65rem;
            padding: 2px 5px;
            border-radius: 8px;
            font-weight: 600;
            margin-left: 6px;
        }
        
        .verification-badge.verified {
            background: rgba(76, 175, 80, 0.15);
            color: #2e7d32;
        }
        
        .verification-badge.unverified {
            background: rgba(255, 152, 0, 0.15);
            color: #e65100;
        }
        
        /* Info tooltip */
        .info-tooltip {
            position: relative;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 20px;
            height: 20px;
            background: var(--bg-elevated);
            border: 1px solid var(--border-default);
            border-radius: 50%;
            font-size: 0.7rem;
            color: var(--text-muted);
            cursor: help;
            margin-left: 8px;
        }
        
        .info-tooltip-content {
            position: absolute;
            bottom: calc(100% + 10px);
            left: 50%;
            transform: translateX(-50%);
            width: 320px;
            padding: 16px;
            background: white;
            border: 1px solid var(--border-default);
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.15);
            font-size: 0.85rem;
            line-height: 1.5;
            color: var(--text-secondary);
            opacity: 0;
            visibility: hidden;
            transition: all 0.2s;
            z-index: 100;
        }
        
        .info-tooltip-content::after {
            content: '';
            position: absolute;
            top: 100%;
            left: 50%;
            transform: translateX(-50%);
            border: 8px solid transparent;
            border-top-color: white;
        }
        
        .info-tooltip:hover .info-tooltip-content {
            opacity: 1;
            visibility: visible;
        }
        
        .info-tooltip-content h4 {
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 8px;
        }
        
        .info-tooltip-content ul {
            margin: 8px 0;
            padding-left: 16px;
        }
        
        .info-tooltip-content li {
            margin: 4px 0;
        }
        
        .info-tooltip-content .badge-demo {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 6px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 500;
        }
        
        .info-tooltip-content .badge-demo.green {
            background: rgba(76, 175, 80, 0.15);
            color: #2e7d32;
        }
        
        .info-tooltip-content .badge-demo.orange {
            background: rgba(255, 152, 0, 0.15);
            color: #e65100;
        }
        
        /* Client verification actions - simplified */
        .client-actions {
            display: none;
        }
        
        .client-action-btn {
            background: none;
            border: none;
            cursor: pointer;
            padding: 4px;
            border-radius: 4px;
            font-size: 0.9rem;
            opacity: 0.7;
            transition: all 0.15s;
        }
        
        .client-action-btn:hover {
            opacity: 1;
            background: rgba(233, 30, 99, 0.1);
        }
        
        .client-action-btn.verify-btn:hover {
            background: rgba(76, 175, 80, 0.15);
        }
        
        .client-action-btn.fix-btn:hover {
            background: rgba(255, 152, 0, 0.15);
        }
        
        /* Verify confirmation popup */
        .verify-popup-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.4);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1001;
            animation: fadeIn 0.15s ease-out;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        
        .verify-popup {
            background: white;
            border-radius: 20px;
            padding: 28px;
            max-width: 360px;
            width: 90%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
            animation: slideUp 0.2s ease-out;
        }
        
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .verify-popup-icon {
            width: 56px;
            height: 56px;
            background: linear-gradient(135deg, #4caf50, #81c784);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 16px;
            font-size: 1.5rem;
            color: white;
        }
        
        .verify-popup h3 {
            font-size: 1.2rem;
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--text-primary);
        }
        
        .verify-popup p {
            margin: 4px 0;
            color: var(--text-secondary);
            font-size: 0.95rem;
        }
        
        .verify-popup-note {
            margin-top: 12px !important;
            font-size: 0.85rem !important;
            color: var(--text-muted) !important;
            line-height: 1.4;
        }
        
        .verify-popup-hint {
            font-size: 0.8rem !important;
            color: var(--text-muted) !important;
            font-style: italic;
            margin-top: 8px !important;
        }
        
        .verify-popup-wide {
            max-width: 450px;
            text-align: left;
        }
        
        .verify-popup-wide h3 {
            text-align: center;
        }
        
        .verify-popup-wide > p:first-of-type {
            text-align: center;
        }
        
        .verify-popup-fields {
            margin: 16px 0;
        }
        
        .verify-popup-fields label {
            display: block;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 6px;
            margin-top: 12px;
        }
        
        .verify-popup-fields label:first-child {
            margin-top: 0;
        }
        
        .verify-popup-fields input,
        .verify-popup-fields textarea {
            width: 100%;
            padding: 10px 12px;
            border: 2px solid #e5e5e5;
            border-radius: 10px;
            font-size: 0.95rem;
            font-family: inherit;
            transition: border-color 0.2s;
            box-sizing: border-box;
        }
        
        .verify-popup-fields input:focus,
        .verify-popup-fields textarea:focus {
            outline: none;
            border-color: var(--primary);
        }
        
        .verify-popup-fields textarea {
            resize: vertical;
            min-height: 70px;
        }
        
        .verify-popup-actions {
            display: flex;
            gap: 12px;
            margin-top: 20px;
        }
        
        .verify-popup-btn {
            flex: 1;
            padding: 12px 16px;
            border-radius: 12px;
            font-weight: 500;
            font-size: 0.95rem;
            cursor: pointer;
            transition: all 0.15s;
        }
        
        .verify-popup-btn.cancel {
            background: var(--bg-elevated);
            border: 1px solid var(--border-default);
            color: var(--text-secondary);
        }
        
        .verify-popup-btn.cancel:hover {
            background: var(--bg-hover);
        }
        
        .verify-popup-btn.confirm {
            background: linear-gradient(135deg, #4caf50, #66bb6a);
            border: none;
            color: white;
        }
        
        .verify-popup-btn.confirm:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 15px rgba(76, 175, 80, 0.3);
        }
        
        /* Fix address modal */
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            opacity: 0;
            visibility: hidden;
            transition: all 0.2s;
        }
        
        .modal-overlay.active {
            opacity: 1;
            visibility: visible;
        }
        
        .modal-content {
            background: white;
            border-radius: 20px;
            padding: 28px;
            max-width: 500px;
            width: 90%;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
        }
        
        .modal-content-wide {
            max-width: 520px;
        }
        
        .modal-bsale-address {
            font-size: 0.9rem;
            color: var(--text-muted);
            margin-bottom: 16px;
            padding: 10px;
            background: #f8f8f8;
            border-radius: 8px;
        }
        
        .modal-field {
            margin-bottom: 14px;
        }
        
        .modal-field label {
            display: block;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }
        
        .modal-field textarea {
            resize: vertical;
            min-height: 70px;
            font-family: inherit;
        }
        
        .modal-title {
            font-size: 1.2rem;
            font-weight: 600;
            margin-bottom: 16px;
            color: var(--text-primary);
        }
        
        .modal-body {
            margin-bottom: 20px;
        }
        
        .modal-body p {
            margin-bottom: 12px;
            color: var(--text-secondary);
            font-size: 0.9rem;
        }
        
        .modal-input {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid var(--border-default);
            border-radius: 12px;
            font-size: 0.95rem;
        }
        
        .modal-input:focus {
            outline: none;
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 4px var(--glow-primary);
        }
        
        .modal-actions {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
        }
        
        .modal-btn {
            padding: 10px 20px;
            border-radius: 10px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s;
        }
        
        .modal-btn-cancel {
            background: var(--bg-elevated);
            border: 1px solid var(--border-default);
            color: var(--text-secondary);
        }
        
        .modal-btn-cancel:hover {
            background: var(--bg-hover);
        }
        
        .modal-btn-save {
            background: linear-gradient(135deg, var(--accent-primary), var(--accent-secondary));
            border: none;
            color: white;
        }
        
        .modal-btn-save:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 15px rgba(233, 30, 99, 0.3);
        }
        
        .no-clients {
            padding: 20px;
            text-align: center;
            color: var(--text-muted);
            font-size: 0.9rem;
        }
        
        .loading-clients {
            padding: 20px;
            text-align: center;
            color: var(--text-secondary);
        }
        
        .loading-clients .mini-spinner {
            width: 20px;
            height: 20px;
            border: 2px solid rgba(233, 30, 99, 0.2);
            border-top-color: #e91e63;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            display: inline-block;
            margin-right: 8px;
            vertical-align: middle;
        }
        
        .progress-container {
            margin-top: 12px;
            padding: 0 4px;
        }
        
        .progress-bar {
            width: 100%;
            height: 6px;
            background: var(--bg-base);
            border-radius: 3px;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #f48fb1, #e91e63);
            border-radius: 3px;
            transition: width 0.3s ease;
        }
        
        .progress-text {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 6px;
            text-align: center;
            font-family: 'JetBrains Mono', monospace;
        }
        
        .loading-label {
            margin-top: 12px;
            font-size: 0.85rem;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        
        .divider {
            display: flex;
            align-items: center;
            gap: 16px;
            margin: 20px 0;
            color: var(--text-muted);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .divider::before,
        .divider::after {
            content: '';
            flex: 1;
            height: 1px;
            background: var(--border-subtle);
        }
        
        /* Footer */
        .app-footer {
            text-align: center;
            padding: 32px 0;
            color: var(--text-muted);
            font-size: 0.8rem;
        }
        
        .app-footer a {
            color: var(--accent-primary);
            text-decoration: none;
        }
        
        .app-footer a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="bg-pattern"></div>
    <div class="grid-overlay"></div>
    
    <div class="app-container">
        <header class="app-header">
            <div class="logo">
                <div class="logo-icon">🐱</div>
                <span class="logo-text">MiuRuta</span>
            </div>
            <p class="tagline">Crea tu ruta de entregas 🐾 <span style="color: var(--text-muted); font-size: 0.9em;">Recuerda verificar las direcciones para mayor precisión</span></p>
        </header>
        
        <main id="form-section">
            <div class="card">
                <div class="card-header">
                    <div class="card-icon">📍</div>
                    <span class="card-title">Puntos de Inicio y Fin</span>
                </div>
                
                <div class="input-row">
                    <div class="input-group">
                        <label class="input-label">
                            <span class="input-label-icon">🟢</span>
                            Punto de Inicio
                        </label>
                        <input type="text" id="start" class="input-field input-prefilled" value="https://maps.app.goo.gl/mk3h6HRg4Mv7ru3GA" placeholder="Pega el link de Google Maps...">
                        <span class="prefilled-label">📍 MiuShop</span>
                    </div>
                    
                    <div class="input-group">
                        <label class="input-label">
                            <span class="input-label-icon">🏁</span>
                            Punto Final
                        </label>
                        <input type="text" id="end" class="input-field" placeholder="Pega el link de Google Maps...">
                        <button type="button" class="quick-fill-btn" onclick="setEndToMiuShop()">
                            <span>🏠</span> Usar MiuShop
                        </button>
                    </div>
                </div>
            </div>
            
            <div class="card card-bsale">
                <div class="card-header">
                    <div class="card-icon">👥</div>
                    <span class="card-title">Clientes</span>
                    <span class="info-tooltip">?
                        <div class="info-tooltip-content">
                            <h4>Sistema de Verificación</h4>
                            <p>Las direcciones de clientes se geocodifican automáticamente pero pueden tener errores.</p>
                            <ul>
                                <li><span class="badge-demo green">●</span> <strong>Verificado</strong> - Dirección confirmada</li>
                                <li><span class="badge-demo orange">●</span> <strong>Sin verificar</strong> - Requiere revisión</li>
                            </ul>
                            <p><strong>Para verificar:</strong></p>
                            <ul>
                                <li>🗺️ Abre en Maps para ver ubicación</li>
                                <li>✓ Si es correcta, marca como verificada</li>
                                <li>✏️ Si es incorrecta, pega el link correcto</li>
                            </ul>
                            <p style="margin-top: 8px; font-size: 0.8rem; color: var(--text-muted);">Los cambios se guardan en Google Sheets.</p>
                        </div>
                    </span>
                    <button type="button" class="refresh-btn" id="refresh-clients-btn" onclick="refreshClients()" title="Sincronizar con Bsale">
                        <span class="refresh-icon">↻</span>
                    </button>
                </div>
                <div class="cache-status" id="cache-status"></div>
                <div class="sync-progress-container" id="sync-progress-container">
                    <div class="sync-progress-bar">
                        <div class="sync-progress-fill" id="sync-progress-fill"></div>
                    </div>
                    <div class="sync-progress-text" id="sync-progress-text"></div>
                </div>
                
                <div class="input-group">
                    <div class="client-selector">
                        <input type="text" id="client-search" class="client-search" placeholder="Buscar por nombre, empresa o dirección..." autocomplete="off">
                        <div class="client-dropdown" id="client-dropdown">
                            <div class="loading-clients" id="loading-clients">
                                <span class="mini-spinner"></span>
                                Cargando clientes...
                            </div>
                        </div>
                    </div>
                    <div class="selected-clients" id="selected-clients"></div>
                </div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <div class="card-icon">📦</div>
                    <span class="card-title">Paradas Adicionales</span>
                </div>
                
                <div class="input-group">
                    <label class="input-label">
                        <span class="input-label-icon">🔗</span>
                        Links de Google Maps (opcional, uno por línea)
                    </label>
                    <textarea id="stops" class="input-field" placeholder="https://maps.app.goo.gl/abc123...
https://maps.app.goo.gl/def456...
https://maps.app.goo.gl/ghi789..."></textarea>
                    <p class="input-hint">
                        <span>💡</span>
                        Agrega paradas adicionales que no estén en Bsale
                    </p>
                </div>
            </div>
            
            <div id="route-button-container">
                <button class="btn btn-primary" id="optimize-btn" onclick="optimizeRoute()">
                    <span>⚡</span>
                    Optimizar Ruta
                </button>
                <p class="unverified-warning" id="unverified-warning" style="display: none;">
                    ⚠️ <span id="unverified-count">0</span> cliente(s) sin verificar. Verifica las direcciones antes de continuar.
                </p>
            </div>
            
            <div id="error-container"></div>
        </main>
        
        <div class="loading-container" id="loading">
            <div class="loader"></div>
            <p class="loading-text">Calculando la ruta más eficiente...</p>
        </div>
        
        <div id="results">
            <div class="card">
                <div class="results-header">
                    <div>
                        <h2 class="results-title">Ruta Optimizada</h2>
                        <p class="results-subtitle">El orden más eficiente para tus entregas</p>
                    </div>
                    <div class="stats-grid">
                        <div class="stat-item">
                            <div class="stat-value" id="total-distance">--</div>
                            <div class="stat-label">Distancia</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-value" id="total-time">--</div>
                            <div class="stat-label">Tiempo</div>
                        </div>
                    </div>
                </div>
                
                <div class="timeline" id="route-timeline">
                    <!-- Populated by JS -->
                </div>
                
                <div class="action-row" id="maps-link-container">
                    <a href="#" class="btn btn-maps" target="_blank">
                        <span>🗺️</span>
                        Abrir en Google Maps
                    </a>
                </div>
                
                <!-- Route Summary for WhatsApp -->
                <div class="route-summary-section" id="route-summary-section">
                    <div class="route-summary-header">
                        <h3>📋 Resumen para WhatsApp</h3>
                        <button class="btn btn-copy" onclick="copyRouteSummary()">
                            <span id="copy-icon">📋</span>
                            <span id="copy-text">Copiar</span>
                        </button>
                    </div>
                    <div class="route-summary-input-row">
                        <label>Título de la ruta:</label>
                        <input type="text" id="route-title-input" placeholder="Ej: Ruta 1 / Santi" value="Ruta del día" onchange="updateRouteSummary()">
                    </div>
                    <textarea class="route-summary-text" id="route-summary-text" readonly></textarea>
                </div>
                
                <div class="action-row">
                    <button class="btn btn-secondary" onclick="resetForm()">
                        <span>↩️</span>
                        Nueva Ruta
                    </button>
                </div>
            </div>
        </div>
        
        <footer class="app-footer">
            Powered by <a href="https://developers.google.com/maps/documentation/routes" target="_blank">Google Routes API</a>
        </footer>
    </div>

    <script>
        // Global state
        let allClients = [];
        let selectedClients = [];
        let sheetsAvailable = false;  // Whether Google Sheets is configured
        let fixingClientId = null;    // Client being fixed in modal
        let currentRouteData = null;  // Store current route data for summary
        const MIUSHOP_URL = 'https://maps.app.goo.gl/mk3h6HRg4Mv7ru3GA';
        
        function setEndToMiuShop() {
            const endInput = document.getElementById('end');
            endInput.value = MIUSHOP_URL;
            endInput.classList.add('input-prefilled');
            
            // Update button to show it's active
            const btn = event.target.closest('.quick-fill-btn');
            if (btn) {
                btn.classList.add('active');
                btn.innerHTML = '<span>✓</span> MiuShop';
            }
        }
        
        // Initialize on page load
        document.addEventListener('DOMContentLoaded', () => {
            loadClients();
            setupClientSearch();
        });
        
        // ============ Address Verification Functions ============
        
        function openMapsLink(client) {
            // Open the client's maps link or generate one from address
            let url = client.maps_link;
            if (!url && client.address) {
                // Generate a search URL from address
                const query = encodeURIComponent(`${client.address}, ${client.district || ''}, ${client.city || ''}, Peru`);
                url = `https://www.google.com/maps/search/?api=1&query=${query}`;
            }
            if (url) {
                window.open(url, '_blank');
            }
        }
        
        // Track which clients are in "confirming" state for 2-step verification
        let confirmingVerification = {};
        
        function handleVerifyClick(clientId) {
            // Show confirmation popup
            const client = selectedClients.find(c => (c.bsale_id || c.id) == clientId);
            const clientName = client ? (client.name || `${client.firstName || ''} ${client.lastName || ''}`.trim()) : 'este cliente';
            
            showVerifyConfirmPopup(clientId, clientName);
        }
        
        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;')
                      .replace(/'/g, '&#039;')
                      .replace(/`/g, '&#96;');
        }
        
        function showVerifyConfirmPopup(clientId, clientName) {
            // Get client details
            const client = selectedClients.find(c => (c.bsale_id || c.id) == clientId);
            const bsaleAddress = client ? (client.address || '') : '';
            const existingCleanAddress = client ? (client.clean_address || '') : '';
            const existingDistrict = client ? (client.verified_district || client.district || '') : '';
            
            // Use existing clean_address or default to bsale address
            const defaultCleanAddress = existingCleanAddress || bsaleAddress;
            
            // Escape values for safe HTML insertion
            const safeClientName = escapeHtml(clientName);
            const safeCleanAddress = escapeHtml(defaultCleanAddress);
            const safeDistrict = escapeHtml(existingDistrict);
            
            // Create popup overlay
            const popup = document.createElement('div');
            popup.className = 'verify-popup-overlay';
            popup.innerHTML = `
                <div class="verify-popup verify-popup-wide">
                    <div class="verify-popup-icon">✓</div>
                    <h3>Verificar dirección</h3>
                    <p><strong>${safeClientName}</strong></p>
                    
                    <div class="verify-popup-fields">
                        <label>Dirección formateada (para WhatsApp):</label>
                        <textarea id="verify-clean-address" rows="3" placeholder="Ej: Av. Benavides 4331&#10;Piso 3B">${safeCleanAddress}</textarea>
                        
                        <label>Distrito:</label>
                        <input type="text" id="verify-district" value="${safeDistrict}" placeholder="Ej: San Isidro, Miraflores">
                    </div>
                    
                    <p class="verify-popup-note">Al verificar confirmas que la ubicación es correcta. Edita el formato de la dirección para que aparezca bien en el resumen de ruta.</p>
                    
                    <div class="verify-popup-actions">
                        <button class="verify-popup-btn cancel" onclick="closeVerifyPopup()">Cancelar</button>
                        <button class="verify-popup-btn confirm" onclick="confirmVerifyWithAddress(${clientId})">✓ Verificar</button>
                    </div>
                </div>
            `;
            document.body.appendChild(popup);
            
            // Close on overlay click
            popup.addEventListener('click', (e) => {
                if (e.target === popup) closeVerifyPopup();
            });
        }
        
        async function confirmVerifyWithAddress(clientId) {
            const cleanAddress = document.getElementById('verify-clean-address').value.trim();
            const verifiedDistrict = document.getElementById('verify-district').value.trim();
            
            closeVerifyPopup();
            
            const btn = document.getElementById(`verify-btn-${clientId}`);
            if (btn) {
                btn.innerHTML = '...';
                btn.disabled = true;
            }
            
            try {
                const response = await fetch(`/api/sheets/clients/${clientId}/verify`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        clean_address: cleanAddress,
                        verified_district: verifiedDistrict 
                    })
                });
                const data = await response.json();
                
                if (data.status === 'success') {
                    // Update local client data
                    const client = selectedClients.find(c => c.bsale_id == clientId);
                    if (client) {
                        client.verified = 'yes';
                        client.clean_address = cleanAddress;
                        client.verified_district = verifiedDistrict;
                    }
                    const allClient = allClients.find(c => c.bsale_id == clientId);
                    if (allClient) {
                        allClient.verified = 'yes';
                        allClient.clean_address = cleanAddress;
                        allClient.verified_district = verifiedDistrict;
                    }
                    renderSelectedClients();
                } else {
                    alert('Error: ' + (data.error || 'No se pudo verificar'));
                    if (btn) {
                        btn.innerHTML = '✓';
                        btn.disabled = false;
                    }
                }
            } catch (e) {
                alert('Error de conexión: ' + e.message);
                if (btn) {
                    btn.innerHTML = '✓';
                    btn.disabled = false;
                }
            }
        }
        
        function closeVerifyPopup() {
            const popup = document.querySelector('.verify-popup-overlay');
            if (popup) popup.remove();
        }
        
        function confirmVerify(clientId) {
            closeVerifyPopup();
            verifyClientAddress(clientId);
        }
        
        async function verifyClientAddress(clientId) {
            const btn = document.getElementById(`verify-btn-${clientId}`);
            if (btn) {
                btn.innerHTML = '...';
                btn.disabled = true;
            }
            
            try {
                const response = await fetch(`/api/sheets/clients/${clientId}/verify`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' }
                });
                const data = await response.json();
                
                if (data.status === 'success') {
                    // Update local client data
                    const client = selectedClients.find(c => c.bsale_id == clientId);
                    if (client) client.verified = 'yes';
                    const allClient = allClients.find(c => c.bsale_id == clientId);
                    if (allClient) allClient.verified = 'yes';
                    
                    renderSelectedClients();
                    renderClientOptions(allClients);
                    showSuccess('✓ Dirección verificada y guardada');
                } else {
                    showError(data.error || 'Error al verificar');
                    if (btn) {
                        btn.innerHTML = '✓';
                        btn.disabled = false;
                    }
                }
            } catch (err) {
                console.error('Verify error:', err);
                showError('Error de conexión');
                if (btn) {
                    btn.innerHTML = '✓';
                    btn.disabled = false;
                }
            }
        }
        
        function openFixModal(client) {
            fixingClientId = client.bsale_id;
            document.getElementById('modal-client-name').textContent = client.name || `${client.firstName || ''} ${client.lastName || ''}`.trim();
            document.getElementById('modal-current-address').textContent = client.address || 'Sin dirección';
            document.getElementById('modal-maps-link').value = client.maps_link || '';
            document.getElementById('modal-clean-address').value = client.clean_address || client.address || '';
            document.getElementById('modal-district').value = client.verified_district || client.district || '';
            document.getElementById('fix-address-modal').classList.add('active');
        }
        
        function closeFixModal() {
            fixingClientId = null;
            document.getElementById('fix-address-modal').classList.remove('active');
        }
        
        async function saveFixedAddress() {
            const mapsLink = document.getElementById('modal-maps-link').value.trim();
            const cleanAddress = document.getElementById('modal-clean-address').value.trim();
            const verifiedDistrict = document.getElementById('modal-district').value.trim();
            
            if (!mapsLink) {
                alert('Por favor ingresa un link de Google Maps');
                return;
            }
            
            try {
                const response = await fetch(`/api/sheets/clients/${fixingClientId}/fix`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        maps_link: mapsLink,
                        clean_address: cleanAddress,
                        verified_district: verifiedDistrict
                    })
                });
                const data = await response.json();
                
                if (data.status === 'success') {
                    // Update local client data
                    const client = selectedClients.find(c => c.bsale_id == fixingClientId);
                    if (client) {
                        client.verified = 'yes';
                        client.maps_link = mapsLink;
                        client.clean_address = cleanAddress;
                        client.verified_district = verifiedDistrict;
                    }
                    const allClient = allClients.find(c => c.bsale_id == fixingClientId);
                    if (allClient) {
                        allClient.verified = 'yes';
                        allClient.maps_link = mapsLink;
                        allClient.clean_address = cleanAddress;
                        allClient.verified_district = verifiedDistrict;
                    }
                    
                    closeFixModal();
                    renderSelectedClients();
                    renderClientOptions(allClients);
                    showSuccess('✓ Dirección corregida y verificada');
                } else {
                    alert(data.error || 'Error al guardar');
                }
            } catch (err) {
                console.error('Fix error:', err);
                alert('Error de conexión');
            }
        }
        
        function showSuccess(message) {
            // Simple success notification
            const errorContainer = document.getElementById('error-container');
            errorContainer.innerHTML = `<div style="background: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); color: #2e7d32; padding: 12px 16px; border-radius: 12px; margin-bottom: 16px;">${message}</div>`;
            setTimeout(() => { errorContainer.innerHTML = ''; }, 3000);
        }
        
        function formatLastUpdated(isoString) {
            if (!isoString) return 'Nunca';
            const date = new Date(isoString);
            const now = new Date();
            const diffMs = now - date;
            const diffMins = Math.floor(diffMs / 60000);
            const diffHours = Math.floor(diffMs / 3600000);
            const diffDays = Math.floor(diffMs / 86400000);
            
            if (diffMins < 1) return 'Hace un momento';
            if (diffMins < 60) return `Hace ${diffMins} min`;
            if (diffHours < 24) return `Hace ${diffHours}h`;
            if (diffDays < 7) return `Hace ${diffDays} días`;
            return date.toLocaleDateString('es-PE', { day: 'numeric', month: 'short' });
        }
        
        function updateCacheStatus(data) {
            const statusEl = document.getElementById('cache-status');
            const refreshBtn = document.getElementById('refresh-clients-btn');
            
            if (data.loading) {
                const progress = data.progress || 0;
                const total = data.total || 0;
                const percent = total > 0 ? Math.round((progress / total) * 100) : 0;
                statusEl.className = 'cache-status loading';
                statusEl.innerHTML = `<span class="status-dot"></span> Actualizando... ${percent}% (${progress}/${total})`;
                refreshBtn.classList.add('loading');
            } else {
                statusEl.className = 'cache-status';
                statusEl.innerHTML = `<span class="status-dot"></span> ${data.count || 0} clientes • Actualizado: ${formatLastUpdated(data.last_updated)}`;
                refreshBtn.classList.remove('loading');
            }
        }
        
        async function loadClients() {
            const loadingEl = document.getElementById('loading-clients');
            
            // Try Google Sheets first (source of truth for verified addresses)
            try {
                const sheetsResponse = await fetch('/api/sheets/clients');
                if (sheetsResponse.ok) {
                    const sheetsData = await sheetsResponse.json();
                    if (sheetsData.clients && sheetsData.clients.length > 0) {
                        sheetsAvailable = true;
                        allClients = sheetsData.clients;
                        loadingEl.style.display = 'none';
                        
                        // Update status for sheets
                        const statusEl = document.getElementById('cache-status');
                        const verifiedCount = allClients.filter(c => c.verified === 'yes').length;
                        statusEl.className = 'cache-status';
                        statusEl.innerHTML = `<span class="status-dot"></span> ${allClients.length} clientes (${verifiedCount} verificados) • Fuente: Google Sheets`;
                        
                        renderClientOptions(allClients);
                        return;
                    }
                }
            } catch (err) {
                console.log('Google Sheets not available, falling back to Bsale cache:', err);
            }
            
            // Fall back to Bsale cache
            try {
                const response = await fetch('/api/clients');
                const data = await response.json();
                allClients = data.clients || [];
                
                // Update cache status
                updateCacheStatus(data);
                
                // If still loading on server, show progress and retry
                if (data.loading) {
                    const progress = data.progress || 0;
                    const total = data.total || 0;
                    const percent = total > 0 ? Math.round((progress / total) * 100) : 0;
                    
                    // If we have cached clients, show them while loading
                    if (allClients.length > 0) {
                        loadingEl.style.display = 'none';
                        renderClientOptions(allClients);
                    } else {
                        loadingEl.innerHTML = `
                            <div class="loading-clients">
                                <div class="progress-container">
                                    <div class="progress-bar">
                                        <div class="progress-fill" style="width: ${percent}%"></div>
                                    </div>
                                    <div class="progress-text">${progress.toLocaleString()} / ${total.toLocaleString()} clientes (${percent}%)</div>
                                </div>
                                <div class="loading-label"><span class="mini-spinner"></span> Cargando clientes de Bsale...</div>
                            </div>
                        `;
                    }
                    setTimeout(loadClients, 1000);
                    return;
                }
                
                if (allClients.length === 0) {
                    loadingEl.innerHTML = '<div class="no-clients">No se encontraron clientes</div>';
                } else {
                    loadingEl.style.display = 'none';
                    renderClientOptions(allClients);
                }
            } catch (err) {
                console.error('Error loading clients:', err);
                document.getElementById('loading-clients').innerHTML = 
                    '<div class="no-clients">Error al cargar clientes</div>';
            }
        }
        
        let syncPollingInterval = null;
        
        async function refreshClients() {
            const refreshBtn = document.getElementById('refresh-clients-btn');
            if (refreshBtn.classList.contains('loading')) return;
            
            refreshBtn.classList.add('loading');
            
            try {
                // Use the new sheets sync endpoint
                const response = await fetch('/api/sheets/sync', { 
                    method: 'POST'
                });
                const data = await response.json();
                
                if (data.status === 'started' || data.status === 'already_syncing') {
                    // Start polling for sync status
                    startSyncPolling();
                }
            } catch (err) {
                console.error('Error starting sync:', err);
                refreshBtn.classList.remove('loading');
            }
        }
        
        function startSyncPolling() {
            if (syncPollingInterval) clearInterval(syncPollingInterval);
            
            const progressContainer = document.getElementById('sync-progress-container');
            const progressFill = document.getElementById('sync-progress-fill');
            const progressText = document.getElementById('sync-progress-text');
            const statusEl = document.getElementById('cache-status');
            
            progressContainer.classList.add('active');
            
            syncPollingInterval = setInterval(async () => {
                try {
                    const response = await fetch('/api/sheets/sync/status');
                    const state = await response.json();
                    
                    // Update UI based on sync state
                    statusEl.className = 'cache-status syncing';
                    
                    if (state.stage === 'fetching_bsale') {
                        statusEl.innerHTML = '<span class="status-dot"></span> Obteniendo clientes de Bsale...';
                        progressFill.style.width = '10%';
                        progressText.textContent = state.message;
                    } else if (state.stage === 'comparing') {
                        statusEl.innerHTML = '<span class="status-dot"></span> Comparando datos...';
                        progressFill.style.width = '30%';
                        progressText.textContent = state.message;
                    } else if (state.stage === 'updating') {
                        statusEl.innerHTML = '<span class="status-dot"></span> Actualizando...';
                        progressFill.style.width = '50%';
                        progressText.textContent = state.message;
                    } else if (state.stage === 'geocoding') {
                        const percent = state.total > 0 ? 50 + Math.round((state.progress / state.total) * 30) : 50;
                        statusEl.innerHTML = `<span class="status-dot"></span> Geocodificando ${state.progress}/${state.total}...`;
                        progressFill.style.width = percent + '%';
                        progressText.textContent = state.message;
                    } else if (state.stage === 'adding') {
                        statusEl.innerHTML = '<span class="status-dot"></span> Guardando...';
                        progressFill.style.width = '90%';
                        progressText.textContent = state.message;
                    } else if (state.stage === 'done') {
                        stopSyncPolling();
                        progressFill.style.width = '100%';
                        progressText.textContent = '¡Listo!';
                        
                        // Show success message
                        let msg = '✓ Sincronización completa';
                        if (state.new_clients > 0) msg += ` • ${state.new_clients} nuevos`;
                        if (state.updated_clients > 0) msg += ` • ${state.updated_clients} actualizados`;
                        statusEl.className = 'cache-status';
                        statusEl.innerHTML = `<span class="status-dot"></span> ${msg}`;
                        
                        // Hide progress bar after a moment and reload clients
                        setTimeout(() => {
                            progressContainer.classList.remove('active');
                            loadClients();  // Reload the client list
                        }, 1500);
                    } else if (state.stage === 'error') {
                        stopSyncPolling();
                        statusEl.className = 'cache-status';
                        statusEl.innerHTML = `<span class="status-dot" style="background:#ef5350;"></span> Error: ${state.error}`;
                        progressContainer.classList.remove('active');
                    }
                    
                    if (!state.syncing && state.stage !== 'done') {
                        stopSyncPolling();
                    }
                } catch (err) {
                    console.error('Sync polling error:', err);
                }
            }, 500);
        }
        
        function stopSyncPolling() {
            if (syncPollingInterval) {
                clearInterval(syncPollingInterval);
                syncPollingInterval = null;
            }
            const refreshBtn = document.getElementById('refresh-clients-btn');
            refreshBtn.classList.remove('loading');
        }
        
        // Track highlighted option index for keyboard navigation
        let highlightedIndex = -1;
        let currentFilteredClients = [];
        
        function setupClientSearch() {
            const searchInput = document.getElementById('client-search');
            const dropdown = document.getElementById('client-dropdown');
            
            searchInput.addEventListener('focus', () => {
                dropdown.classList.add('active');
                highlightedIndex = -1;
            });
            
            // Keyboard navigation
            searchInput.addEventListener('keydown', (e) => {
                const options = dropdown.querySelectorAll('.client-option');
                
                if (e.key === 'Escape') {
                    // Close dropdown and blur input
                    dropdown.classList.remove('active');
                    searchInput.blur();
                    highlightedIndex = -1;
                    updateHighlight(options);
                    e.preventDefault();
                    return;
                }
                
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    highlightedIndex = Math.min(highlightedIndex + 1, options.length - 1);
                    updateHighlight(options);
                    scrollOptionIntoView(options[highlightedIndex]);
                    return;
                }
                
                if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    highlightedIndex = Math.max(highlightedIndex - 1, 0);
                    updateHighlight(options);
                    scrollOptionIntoView(options[highlightedIndex]);
                    return;
                }
                
                if (e.key === 'Enter' && highlightedIndex >= 0) {
                    e.preventDefault();
                    if (currentFilteredClients[highlightedIndex]) {
                        toggleClient(currentFilteredClients[highlightedIndex]);
                        highlightedIndex = -1;
                        // Collapse dropdown and clear search after selection
                        dropdown.classList.remove('active');
                        searchInput.value = '';
                        searchInput.blur();
                    }
                    return;
                }
            });
            
            searchInput.addEventListener('input', (e) => {
                const query = e.target.value.toLowerCase().trim();
                highlightedIndex = -1; // Reset highlight on new search
                
                if (!query) {
                    currentFilteredClients = allClients.slice(0, 50);
                    renderClientOptions(allClients);
                    return;
                }
                
                // Token-based fuzzy search: "Javier Gutierrez" matches "Javier Alonso Gutierrez"
                // Also ignores accents: "García" matches "Garcia"
                const normalizeText = (text) => text.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
                const normalizedQuery = normalizeText(query);
                const searchTokens = normalizedQuery.split(/\s+/).filter(t => t.length > 0);
                
                const filtered = allClients.filter(c => {
                    // Build searchable text from all relevant fields (handles both Bsale and Sheets format)
                    const clientName = c.name || `${c.firstName || ''} ${c.lastName || ''}`.trim();
                    const searchText = normalizeText([
                        clientName,
                        c.company || '',
                        c.address || '',
                        c.code || '',
                        c.district || ''
                    ].join(' '));
                    
                    // All search tokens must appear somewhere in the searchText
                    return searchTokens.every(token => searchText.includes(token));
                });
                
                // Sort results: exact matches first, then by how early the match appears
                filtered.sort((a, b) => {
                    const aName = normalizeText(a.name || `${a.firstName || ''} ${a.lastName || ''}`);
                    const bName = normalizeText(b.name || `${b.firstName || ''} ${b.lastName || ''}`);
                    const aExact = aName.startsWith(searchTokens[0]);
                    const bExact = bName.startsWith(searchTokens[0]);
                    if (aExact && !bExact) return -1;
                    if (!aExact && bExact) return 1;
                    return aName.localeCompare(bName);
                });
                
                currentFilteredClients = filtered.slice(0, 50);
                renderClientOptions(filtered);
            });
            
            // Close dropdown when clicking outside
            document.addEventListener('click', (e) => {
                if (!e.target.closest('.client-selector')) {
                    dropdown.classList.remove('active');
                    highlightedIndex = -1;
                }
            });
        }
        
        function updateHighlight(options) {
            options.forEach((opt, i) => {
                if (i === highlightedIndex) {
                    opt.classList.add('highlighted');
                } else {
                    opt.classList.remove('highlighted');
                }
            });
        }
        
        function scrollOptionIntoView(option) {
            if (option) {
                option.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }
        }
        
        function renderClientOptions(clients) {
            const dropdown = document.getElementById('client-dropdown');
            
            dropdown.innerHTML = '';
            
            if (clients.length === 0) {
                dropdown.innerHTML = '<div class="no-clients">No se encontraron clientes</div>';
                return;
            }
            
            // Limit to first 50 for performance
            const displayClients = clients.slice(0, 50);
            
            displayClients.forEach(client => {
                const clientId = client.bsale_id || client.id;
                const isSelected = selectedClients.some(c => (c.bsale_id || c.id) === clientId);
                const isVerified = client.verified === 'yes';
                const clientName = client.name || `${client.firstName || ''} ${client.lastName || ''}`.trim();
                
                const div = document.createElement('div');
                div.className = 'client-option' + (isSelected ? ' selected' : '');
                
                // Compact status indicator
                const statusDot = sheetsAvailable 
                    ? `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${isVerified ? '#4caf50' : '#ff9800'};margin-right:8px;"></span>`
                    : '';
                
                // For verified clients, show clean_address if available
                let addressText;
                if (isVerified && client.clean_address) {
                    const district = client.verified_district || client.district || '';
                    addressText = district ? `${client.clean_address} • ${district}` : client.clean_address;
                } else {
                    addressText = [client.address, client.district].filter(Boolean).join(', ');
                }
                
                div.innerHTML = `
                    <div class="client-name">${statusDot}${escapeHtml(clientName)}</div>
                    ${addressText ? `<div class="client-address">${escapeHtml(addressText)}</div>` : ''}
                `;
                div.onclick = () => toggleClient(client);
                dropdown.appendChild(div);
            });
            
            // Show count if more results
            if (clients.length > 50) {
                const moreDiv = document.createElement('div');
                moreDiv.className = 'no-clients';
                moreDiv.textContent = `+ ${clients.length - 50} más. Escribe para filtrar.`;
                dropdown.appendChild(moreDiv);
            }
        }
        
        function toggleClient(client) {
            const clientId = client.bsale_id || client.id;
            const idx = selectedClients.findIndex(c => (c.bsale_id || c.id) === clientId);
            if (idx >= 0) {
                selectedClients.splice(idx, 1);
            } else {
                selectedClients.push(client);
                // Clear search field when selecting a client
                document.getElementById('client-search').value = '';
            }
            renderSelectedClients();
            
            // Show all clients after selection
            renderClientOptions(allClients);
        }
        
        function renderSelectedClients() {
            const container = document.getElementById('selected-clients');
            container.innerHTML = selectedClients.map(c => {
                const clientId = c.bsale_id || c.id;
                const clientName = c.name || `${c.firstName || ''} ${c.lastName || ''}`.trim();
                const isVerified = c.verified === 'yes';
                
                // For verified clients, prefer clean_address; otherwise show bsale address
                let addressText;
                if (isVerified && c.clean_address) {
                    // Show formatted address with verified district
                    const district = c.verified_district || c.district || '';
                    addressText = district ? `${c.clean_address} • ${district}` : c.clean_address;
                } else {
                    // Show raw bsale address
                    addressText = [c.address, c.district].filter(Boolean).join(', ') || 'Sin dirección';
                }
                
                const phoneText = c.phone || '';
                
                // Phone display with click-to-call link
                const phoneHtml = phoneText 
                    ? `<span class="client-tag-phone">📞 <a href="tel:${phoneText}">${phoneText}</a></span>`
                    : '';
                
                // Clean, compact tag with action buttons
                const actionButtons = sheetsAvailable ? `
                    <span class="client-tag-actions">
                        <button class="client-tag-btn maps-btn" onclick="event.stopPropagation(); openMapsLink(selectedClients.find(x => (x.bsale_id||x.id)==${clientId}))" title="Ver en Maps">🗺️</button>
                        ${!isVerified ? `<button class="client-tag-btn verify-btn" id="verify-btn-${clientId}" onclick="event.stopPropagation(); handleVerifyClick(${clientId})" title="Clic para confirmar verificación">✓</button>` : '<button class="client-tag-btn" style="opacity:0.3;cursor:default;background:#e8f5e9;border-color:#c8e6c9;" disabled title="Verificado">✓</button>'}
                        <button class="client-tag-btn fix-btn" onclick="event.stopPropagation(); openFixModal(selectedClients.find(x => (x.bsale_id||x.id)==${clientId}))" title="Corregir dirección">✏️</button>
                    </span>
                ` : '';
                
                return `
                    <div class="client-tag ${isVerified ? 'verified' : 'unverified'}">
                        <span class="client-tag-status ${isVerified ? 'verified' : 'unverified'}"></span>
                        <div class="client-tag-info">
                            <span class="client-tag-name">${clientName}</span>
                            <span class="client-tag-address">${escapeHtml(addressText)}</span>
                            ${phoneHtml}
                        </div>
                        ${actionButtons}
                        <button class="client-tag-remove" onclick="removeClient(${clientId})">×</button>
                    </div>
                `;
            }).join('');
            
            // Update button state based on verification status
            updateOptimizeButtonState();
        }
        
        function updateOptimizeButtonState() {
            const btn = document.getElementById('optimize-btn');
            const warning = document.getElementById('unverified-warning');
            const countSpan = document.getElementById('unverified-count');
            
            if (!sheetsAvailable || selectedClients.length === 0) {
                // If sheets not available or no clients, allow route generation
                btn.disabled = false;
                warning.style.display = 'none';
                return;
            }
            
            // Count unverified clients
            const unverifiedClients = selectedClients.filter(c => c.verified !== 'yes');
            const unverifiedCount = unverifiedClients.length;
            
            if (unverifiedCount > 0) {
                btn.disabled = true;
                countSpan.textContent = unverifiedCount;
                warning.style.display = 'flex';
            } else {
                btn.disabled = false;
                warning.style.display = 'none';
            }
        }
        
        function removeClient(clientId) {
            selectedClients = selectedClients.filter(c => (c.bsale_id || c.id) !== clientId);
            renderSelectedClients();
            renderClientOptions(allClients);
        }
        
        async function optimizeRoute() {
            const startUrl = document.getElementById('start').value.trim();
            const endUrl = document.getElementById('end').value.trim();
            const stopsText = document.getElementById('stops').value.trim();
            
            // Get manual URL stops
            const manualStops = stopsText ? stopsText.split('\\n').filter(url => url.trim()) : [];
            
            // Get selected client IDs (handle both Bsale cache and Sheets format)
            const clientIds = selectedClients.map(c => c.bsale_id || c.id);
            
            // If using Sheets data, also pass the maps links directly for verified clients
            const clientMapsLinks = sheetsAvailable 
                ? selectedClients.filter(c => c.maps_link && c.verified === 'yes').map(c => c.maps_link)
                : [];
            
            // Need at least one stop (client or manual)
            if (clientIds.length === 0 && manualStops.length === 0) {
                showError('Selecciona al menos un cliente o agrega un link de Google Maps');
                return;
            }
            
            if (!startUrl || !endUrl) {
                showError('Por favor ingresa los puntos de inicio y fin');
                return;
            }
            
            // Show loading
            document.getElementById('form-section').style.display = 'none';
            document.getElementById('loading').classList.add('active');
            document.getElementById('results').style.display = 'none';
            document.getElementById('error-container').innerHTML = '';
            
            try {
                const response = await fetch('/optimize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({
                        start: startUrl,
                        end: endUrl,
                        stops: manualStops,
                        clientIds: clientIds
                    })
                });
                
                const data = await response.json();
                
                document.getElementById('loading').classList.remove('active');
                
                if (data.error) {
                    showError(data.error);
                    document.getElementById('form-section').style.display = 'block';
                    return;
                }
                
                displayResults(data);
                
            } catch (err) {
                document.getElementById('loading').classList.remove('active');
                document.getElementById('form-section').style.display = 'block';
                showError('Error de conexión: ' + err.message);
            }
        }
        
        function displayResults(data) {
            document.getElementById('results').style.display = 'block';
            
            // Store for summary generation
            currentRouteData = data;
            
            // Update stats
            document.getElementById('total-distance').textContent = data.total_distance;
            document.getElementById('total-time').textContent = data.total_time;
            
            // Build timeline
            const timeline = document.getElementById('route-timeline');
            timeline.innerHTML = '';
            
            // Start
            timeline.innerHTML += `
                <div class="timeline-item start">
                    <div class="timeline-marker">A</div>
                    <div class="timeline-content">
                        <div class="timeline-info">
                            <div class="timeline-label">Inicio</div>
                            <div class="timeline-address">${data.origin_address}</div>
                            <div class="timeline-coords">${data.origin[0].toFixed(6)}, ${data.origin[1].toFixed(6)}</div>
                        </div>
                    </div>
                </div>
            `;
            
            // Stops
            data.stops.forEach((stop, i) => {
                timeline.innerHTML += `
                    <div class="timeline-item">
                        <div class="timeline-marker">${i + 1}</div>
                        <div class="timeline-content">
                            <div class="timeline-info">
                                <div class="timeline-label">Parada ${i + 1}</div>
                                <div class="timeline-address">${stop.address}</div>
                                <div class="timeline-coords">${stop.coords[0].toFixed(6)}, ${stop.coords[1].toFixed(6)}</div>
                            </div>
                            <div class="timeline-metrics">
                                <div class="metric-distance">${stop.distance}</div>
                                <div class="metric-time">${stop.time}</div>
                            </div>
                        </div>
                    </div>
                `;
            });
            
            // End
            timeline.innerHTML += `
                <div class="timeline-item end">
                    <div class="timeline-marker">B</div>
                    <div class="timeline-content">
                        <div class="timeline-info">
                            <div class="timeline-label">Destino Final</div>
                            <div class="timeline-address">${data.destination_address}</div>
                            <div class="timeline-coords">${data.destination[0].toFixed(6)}, ${data.destination[1].toFixed(6)}</div>
                        </div>
                        <div class="timeline-metrics">
                            <div class="metric-distance">${data.last_leg_distance}</div>
                            <div class="metric-time">${data.last_leg_time}</div>
                        </div>
                    </div>
                </div>
            `;
            
            // Maps links - handle single or multiple route parts
            const mapsLinkContainer = document.getElementById('maps-link-container');
            
            if (data.route_parts && data.route_parts.length > 1) {
                // Multiple route parts needed
                mapsLinkContainer.innerHTML = `
                    <div class="route-parts-notice">
                        <span>📍</span> La ruta tiene ${data.stops.length} paradas y se divide en ${data.route_parts.length} partes
                    </div>
                    <div class="route-parts-buttons">
                        ${data.route_parts.map((part, i) => `
                            <a href="${part.url}" class="btn btn-maps route-part-btn" target="_blank">
                                <span>🗺️</span>
                                Ruta Parte ${part.part_number}
                            </a>
                        `).join('')}
                    </div>
                `;
            } else {
                // Single route
                mapsLinkContainer.innerHTML = `
                    <a href="${data.google_maps_url}" class="btn btn-maps" target="_blank">
                        <span>🗺️</span>
                        Abrir en Google Maps
                    </a>
                `;
            }
            
            // Generate route summary for WhatsApp
            generateRouteSummary(data);
        }
        
        function generateRouteSummary(data) {
            const titleInput = document.getElementById('route-title-input');
            const summaryText = document.getElementById('route-summary-text');
            
            const title = titleInput.value || 'Ruta del día';
            let summary = `*${title}*\\n\\n`;
            
            // Add each stop with number
            let stopNumber = 1;
            data.stops.forEach((stop, i) => {
                if (stop.is_client && stop.client_name) {
                    // Format client stop for WhatsApp with number
                    summary += `*${stopNumber}.* ${stop.client_name}\\n`;
                    
                    // Add phone if available
                    if (stop.phone) {
                        summary += `${stop.phone}\\n`;
                    }
                    
                    // Use clean_address if available, otherwise fall back to address extraction
                    if (stop.clean_address) {
                        summary += `${stop.clean_address}\\n`;
                    } else {
                        // Extract just the address part (after the name and dash)
                        const addressParts = stop.address.split(' - ');
                        if (addressParts.length > 1) {
                            summary += `${addressParts.slice(1).join(' - ')}\\n`;
                        }
                    }
                    
                    // District on separate line
                    if (stop.district) {
                        summary += `${stop.district}\\n`;
                    }
                    
                    summary += '\\n';
                    stopNumber++;
                } else {
                    // Manual waypoint
                    summary += `*${stopNumber}.* Parada manual\\n`;
                    summary += `${stop.address}\\n\\n`;
                    stopNumber++;
                }
            });
            
            // Add Google Maps links
            if (data.route_parts && data.route_parts.length > 1) {
                summary += '---\\n\\n';
                data.route_parts.forEach(part => {
                    summary += `${part.url}\\n\\n`;
                });
            } else if (data.google_maps_url) {
                summary += '---\\n\\n';
                summary += `${data.google_maps_url}\\n`;
            }
            
            summaryText.value = summary.trim();
        }
        
        function updateRouteSummary() {
            if (currentRouteData) {
                generateRouteSummary(currentRouteData);
            }
        }
        
        async function copyRouteSummary() {
            const summaryText = document.getElementById('route-summary-text').value;
            const copyBtn = document.querySelector('.btn-copy');
            const copyIcon = document.getElementById('copy-icon');
            const copyText = document.getElementById('copy-text');
            
            try {
                await navigator.clipboard.writeText(summaryText);
                
                // Visual feedback
                copyBtn.classList.add('copied');
                copyIcon.textContent = '✓';
                copyText.textContent = '¡Copiado!';
                
                setTimeout(() => {
                    copyBtn.classList.remove('copied');
                    copyIcon.textContent = '📋';
                    copyText.textContent = 'Copiar';
                }, 2000);
            } catch (err) {
                // Fallback for older browsers
                const textarea = document.getElementById('route-summary-text');
                textarea.select();
                document.execCommand('copy');
                
                copyBtn.classList.add('copied');
                copyIcon.textContent = '✓';
                copyText.textContent = '¡Copiado!';
                
                setTimeout(() => {
                    copyBtn.classList.remove('copied');
                    copyIcon.textContent = '📋';
                    copyText.textContent = 'Copiar';
                }, 2000);
            }
        }
        
        function showError(message) {
            document.getElementById('error-container').innerHTML = `
                <div class="error-banner">${message}</div>
            `;
        }
        
        function resetForm() {
            document.getElementById('results').style.display = 'none';
            document.getElementById('form-section').style.display = 'block';
            document.getElementById('error-container').innerHTML = '';
            currentRouteData = null;
            document.getElementById('route-summary-text').value = '';
            document.getElementById('route-title-input').value = 'Ruta del día';
        }
    </script>
    
    <!-- Fix Address Modal -->
    <div class="modal-overlay" id="fix-address-modal">
        <div class="modal-content modal-content-wide">
            <h3 class="modal-title">📍 Corregir Dirección</h3>
            <div class="modal-body">
                <p><strong id="modal-client-name"></strong></p>
                <p class="modal-bsale-address">Dirección Bsale: <span id="modal-current-address"></span></p>
                
                <div class="modal-field">
                    <label>Link de Google Maps:</label>
                    <input type="text" class="modal-input" id="modal-maps-link" placeholder="https://maps.app.goo.gl/...">
                </div>
                
                <div class="modal-field">
                    <label>Dirección formateada (para WhatsApp):</label>
                    <textarea class="modal-input" id="modal-clean-address" rows="3" placeholder="Ej: Av. Benavides 4331&#10;Piso 3B"></textarea>
                </div>
                
                <div class="modal-field">
                    <label>Distrito:</label>
                    <input type="text" class="modal-input" id="modal-district" placeholder="Ej: Miraflores, San Isidro">
                </div>
            </div>
            <div class="modal-actions">
                <button class="modal-btn modal-btn-cancel" onclick="closeFixModal()">Cancelar</button>
                <button class="modal-btn modal-btn-save" onclick="saveFixedAddress()">Guardar y Verificar</button>
            </div>
        </div>
    </div>
</body>
</html>
"""


@app.route('/')
@requires_auth
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/clients')
@requires_auth
def get_clients():
    """Get clients from cache."""
    clients = CLIENTS_CACHE["clients"]
    return jsonify({
        "clients": clients,
        "loading": CLIENTS_CACHE["loading"],
        "loaded": CLIENTS_CACHE["loaded"],
        "count": len(clients),
        "progress": CLIENTS_CACHE["loading_progress"],
        "total": CLIENTS_CACHE["total_count"],
        "last_updated": CLIENTS_CACHE["last_updated"]
    })


@app.route('/api/clients/refresh', methods=['POST'])
@requires_auth
def refresh_clients():
    """Force refresh clients from Bsale API."""
    if CLIENTS_CACHE["loading"]:
        return jsonify({"status": "already_loading", "message": "Ya se está actualizando"})
    
    # Start background refresh
    thread = threading.Thread(target=fetch_bsale_clients_from_api)
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "started", "message": "Actualizando clientes..."})


# ============================================================================
# Google Sheets Client Verification Endpoints
# ============================================================================

@app.route('/api/sheets/clients')
@requires_auth
def get_sheets_clients():
    """Get clients from Google Sheet (source of truth for addresses)."""
    try:
        from sheets import get_all_clients
        clients = get_all_clients()
        return jsonify({
            "clients": clients,
            "count": len(clients),
            "source": "google_sheets"
        })
    except ImportError:
        return jsonify({"error": "Google Sheets module not available"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sheets/clients/<int:bsale_id>')
@requires_auth  
def get_sheets_client(bsale_id):
    """Get a single client from Google Sheet by Bsale ID."""
    try:
        from sheets import get_client_by_bsale_id
        client = get_client_by_bsale_id(bsale_id)
        if client:
            return jsonify({"client": client})
        return jsonify({"error": "Cliente no encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sheets/clients/<int:bsale_id>/verify', methods=['POST'])
@requires_auth
def verify_client_address(bsale_id):
    """Mark a client's address as verified in Google Sheet."""
    try:
        from sheets import verify_client
        data = request.json or {}
        clean_address = data.get('clean_address')
        verified_district = data.get('verified_district')
        
        success = verify_client(bsale_id, clean_address=clean_address, verified_district=verified_district)
        if success:
            return jsonify({"status": "success", "message": "Dirección verificada"})
        return jsonify({"error": "No se pudo verificar la dirección"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sheets/clients/<int:bsale_id>/fix', methods=['POST'])
@requires_auth
def fix_client_address(bsale_id):
    """Update a client's Google Maps link and mark as verified."""
    data = request.json
    maps_link = data.get('maps_link', '')
    
    if not maps_link:
        return jsonify({"error": "Se requiere maps_link"}), 400
    
    # Validate it's a Google Maps URL
    if 'google.com/maps' not in maps_link and 'goo.gl' not in maps_link and 'maps.app' not in maps_link:
        return jsonify({"error": "El link debe ser una URL de Google Maps"}), 400
    
    clean_address = data.get('clean_address')
    verified_district = data.get('verified_district')
    
    try:
        from sheets import fix_client_address as fix_address
        success = fix_address(bsale_id, maps_link, clean_address=clean_address, verified_district=verified_district)
        if success:
            return jsonify({"status": "success", "message": "Dirección corregida y verificada"})
        return jsonify({"error": "No se pudo actualizar la dirección"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sheets/sync', methods=['POST'])
@requires_auth
def sync_bsale_to_sheets():
    """Trigger a sync from Bsale to Google Sheets with progress tracking."""
    global SYNC_STATE
    
    if SYNC_STATE["syncing"]:
        return jsonify({"status": "already_syncing", "message": "Ya se está sincronizando"})
    
    try:
        def run_sync_with_progress():
            global SYNC_STATE
            SYNC_STATE = {
                "syncing": True,
                "stage": "fetching_bsale",
                "progress": 0,
                "total": 0,
                "message": "Conectando con Bsale...",
                "new_clients": 0,
                "updated_clients": 0,
                "error": None
            }
            
            try:
                from sheets import get_all_clients, get_existing_bsale_ids, add_clients, batch_update_client_details
                from sync_clients import fetch_all_bsale_clients, geocode_address
                
                # Step 1: Fetch from Bsale
                SYNC_STATE["message"] = "Obteniendo clientes de Bsale..."
                bsale_clients = fetch_all_bsale_clients()
                
                if not bsale_clients:
                    SYNC_STATE["stage"] = "error"
                    SYNC_STATE["error"] = "No se pudieron obtener clientes de Bsale"
                    SYNC_STATE["syncing"] = False
                    return
                
                SYNC_STATE["total"] = len(bsale_clients)
                SYNC_STATE["stage"] = "comparing"
                SYNC_STATE["message"] = "Comparando con base de datos..."
                
                # Step 2: Get existing IDs from sheets
                existing_ids = get_existing_bsale_ids()
                
                # Separate new and existing
                new_clients = [c for c in bsale_clients if str(c.get("bsale_id")) not in existing_ids]
                existing_clients = [c for c in bsale_clients if str(c.get("bsale_id")) in existing_ids]
                
                SYNC_STATE["new_clients"] = len(new_clients)
                
                # Step 3: Update existing clients (if any changed)
                if existing_clients:
                    SYNC_STATE["stage"] = "updating"
                    SYNC_STATE["message"] = f"Verificando cambios en {len(existing_clients)} clientes..."
                    updated = batch_update_client_details(existing_clients)
                    SYNC_STATE["updated_clients"] = updated
                
                # Step 4: Add new clients with geocoding
                if new_clients:
                    SYNC_STATE["stage"] = "geocoding"
                    SYNC_STATE["message"] = f"Geocodificando {len(new_clients)} clientes nuevos..."
                    
                    for i, client in enumerate(new_clients):
                        address = client.get("address", "")
                        city = client.get("city", "")
                        district = client.get("district", "")
                        
                        if address:
                            maps_link = geocode_address(address, city, district)
                            client["maps_link"] = maps_link
                        else:
                            client["maps_link"] = ""
                        
                        SYNC_STATE["progress"] = i + 1
                        SYNC_STATE["message"] = f"Geocodificando {i + 1}/{len(new_clients)}..."
                        time.sleep(0.05)  # Rate limiting
                    
                    SYNC_STATE["stage"] = "adding"
                    SYNC_STATE["message"] = "Agregando clientes nuevos..."
                    add_clients(new_clients)
                
                SYNC_STATE["stage"] = "done"
                SYNC_STATE["message"] = "¡Sincronización completa!"
                SYNC_STATE["syncing"] = False
                
            except Exception as e:
                SYNC_STATE["stage"] = "error"
                SYNC_STATE["error"] = str(e)
                SYNC_STATE["syncing"] = False
        
        thread = threading.Thread(target=run_sync_with_progress)
        thread.daemon = True
        thread.start()
        
        return jsonify({"status": "started", "message": "Sincronización iniciada"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sheets/sync/status')
@requires_auth
def get_sync_status():
    """Get current sync status."""
    return jsonify(SYNC_STATE)


@app.route('/optimize', methods=['POST'])
@requires_auth
def optimize():
    data = request.json
    
    start_url = data.get('start', '')
    end_url = data.get('end', '')
    stop_urls = data.get('stops', [])
    client_ids = data.get('clientIds', [])
    
    # Parse coordinates
    origin = extract_coords_from_url(start_url)
    if not origin:
        return jsonify({"error": f"No se pudo extraer coordenadas del inicio: {start_url}"})
    
    destination = extract_coords_from_url(end_url)
    if not destination:
        return jsonify({"error": f"No se pudo extraer coordenadas del fin: {end_url}"})
    
    waypoints = []
    waypoint_info = []  # Store extra info for each waypoint
    
    # Try to get clients from Google Sheets first (has verified addresses)
    sheets_clients = {}
    try:
        from sheets import get_all_clients
        all_sheets_clients = get_all_clients()
        sheets_clients = {str(c.get('bsale_id')): c for c in all_sheets_clients}
    except Exception as e:
        print(f"Could not load sheets clients: {e}")
    
    # Get coordinates from clients
    if client_ids:
        # Fall back to Bsale cache if sheets not available
        bsale_clients = fetch_bsale_clients()
        bsale_map = {c['id']: c for c in bsale_clients}
        
        for client_id in client_ids:
            coords = None
            client_name = ""
            client_address = ""
            client_phone = ""
            client_clean_address = ""
            client_district = ""
            
            # First, try Google Sheets (verified addresses with maps_link)
            sheets_client = sheets_clients.get(str(client_id))
            if sheets_client:
                client_name = sheets_client.get('name', '')
                client_address = sheets_client.get('address', '')
                client_phone = sheets_client.get('phone', '')
                client_clean_address = sheets_client.get('clean_address', '')
                client_district = sheets_client.get('verified_district', '') or sheets_client.get('district', '')
                
                # Use maps_link if available (especially for verified clients)
                maps_link = sheets_client.get('maps_link', '')
                if maps_link:
                    coords = extract_coords_from_url(maps_link)
                
                # If no maps_link or couldn't extract coords, use stored lat/lng
                if not coords and sheets_client.get('lat') and sheets_client.get('lng'):
                    try:
                        coords = (float(sheets_client['lat']), float(sheets_client['lng']))
                    except (ValueError, TypeError):
                        pass
                
                # Fall back to geocoding address
                if not coords and client_address:
                    coords = geocode_address(
                        client_address,
                        sheets_client.get('city', ''),
                        sheets_client.get('district', '')
                    )
            
            # Fall back to Bsale cache client
            if not coords:
                bsale_client = bsale_map.get(client_id)
                if bsale_client:
                    client_name = f"{bsale_client.get('firstName', '')} {bsale_client.get('lastName', '')}".strip()
                    client_address = bsale_client.get('address', '')
                    client_phone = bsale_client.get('phone', '')
                    client_district = bsale_client.get('district', '')
                    coords = geocode_address(
                        client_address,
                        bsale_client.get('city', ''),
                        bsale_client.get('district', '')
                    )
            
            if coords:
                waypoints.append(coords)
                waypoint_info.append({
                    'coords': coords,
                    'client_name': client_name,
                    'address': client_address,
                    'phone': client_phone,
                    'clean_address': client_clean_address,
                    'district': client_district,
                    'is_client': True
                })
    
    # Get coordinates from manual URLs
    for url in stop_urls:
        coords = extract_coords_from_url(url)
        if coords:
            waypoints.append(coords)
            waypoint_info.append({
                'coords': coords,
                'client_name': None,
                'address': None,
                'phone': None,
                'clean_address': None,
                'district': None,
                'is_client': False
            })
    
    if not waypoints:
        return jsonify({"error": "No se encontraron paradas válidas"})
    
    # Call Routes API
    result = optimize_route(origin, destination, waypoints)
    
    if "error" in result:
        return jsonify({"error": result["error"]})
    
    if "routes" not in result:
        return jsonify({"error": "No se pudo calcular la ruta"})
    
    route = result["routes"][0]
    optimized_order = route.get("optimizedIntermediateWaypointIndex", list(range(len(waypoints))))
    
    # Reorder waypoints and their info
    ordered_waypoints = [waypoints[i] for i in optimized_order]
    ordered_info = [waypoint_info[i] for i in optimized_order]
    
    # Get totals
    total_distance = route.get("distanceMeters", 0)
    total_duration_str = route.get("duration", "0s")
    total_duration = int(total_duration_str.rstrip("s"))
    
    # Reverse geocode all points to get addresses
    origin_address = reverse_geocode(origin)
    destination_address = reverse_geocode(destination)
    
    # Build response
    legs = route.get("legs", [])
    stops_data = []
    for i, (coords, info) in enumerate(zip(ordered_waypoints, ordered_info)):
        # Use client info if available, otherwise reverse geocode
        if info.get('is_client') and info.get('client_name'):
            address_display = f"{info['client_name']} - {info['address']}"
        else:
            address_display = reverse_geocode(coords)
        
        leg_info = {
            "coords": coords,
            "address": address_display,
            "distance": "--",
            "time": "--",
            # Include client details for route summary
            "client_name": info.get('client_name'),
            "phone": info.get('phone'),
            "clean_address": info.get('clean_address'),
            "district": info.get('district'),
            "is_client": info.get('is_client', False)
        }
        if i < len(legs):
            leg = legs[i]
            leg_dist = leg.get("distanceMeters", 0)
            leg_dur_str = leg.get("duration", "0s")
            leg_dur = int(leg_dur_str.rstrip("s"))
            leg_info["distance"] = format_distance(leg_dist)
            leg_info["time"] = format_duration(leg_dur)
        stops_data.append(leg_info)
    
    # Last leg info
    last_leg_dist = "--"
    last_leg_time = "--"
    if legs:
        last_leg = legs[-1]
        last_leg_dist = format_distance(last_leg.get("distanceMeters", 0))
        last_dur_str = last_leg.get("duration", "0s")
        last_leg_time = format_duration(int(last_dur_str.rstrip("s")))
    
    # Generate route URLs - split if more than 8 waypoints
    route_parts = generate_split_routes(origin, destination, ordered_waypoints)
    
    return jsonify({
        "origin": origin,
        "origin_address": origin_address,
        "destination": destination,
        "destination_address": destination_address,
        "stops": stops_data,
        "total_distance": format_distance(total_distance),
        "total_time": format_duration(total_duration),
        "last_leg_distance": last_leg_dist,
        "last_leg_time": last_leg_time,
        "google_maps_url": route_parts[0]["url"] if len(route_parts) == 1 else None,
        "route_parts": route_parts if len(route_parts) > 1 else None,
        "total_route_parts": len(route_parts)
    })


if __name__ == '__main__':
    # Preload Bsale clients in background on startup
    preload_clients()
    app.run(debug=True, port=5000)
