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


def geocode_address(address: str, city: str = "", municipality: str = "") -> tuple[float, float] | None:
    """Convert an address string to coordinates using Google Geocoding API."""
    if not GOOGLE_API_KEY:
        return None
    
    # Build full address string
    full_address = address
    if municipality:
        full_address += f", {municipality}"
    if city:
        full_address += f", {city}"
    full_address += ", Lima, Peru"
    
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
                    "municipality": client.get("municipality", ""),
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
    """Load clients from file first, then refresh from API in background."""
    global CLIENTS_CACHE
    
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
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
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
        
        .client-option:hover {
            background: var(--bg-hover);
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
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 12px;
        }
        
        .client-tag {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 14px;
            background: linear-gradient(135deg, rgba(244, 143, 177, 0.2), rgba(233, 30, 99, 0.1));
            border: 1px solid rgba(233, 30, 99, 0.25);
            border-radius: 20px;
            font-size: 0.85rem;
            color: var(--text-primary);
        }
        
        .client-tag-remove {
            background: none;
            border: none;
            color: var(--accent-primary);
            cursor: pointer;
            font-size: 1rem;
            line-height: 1;
            padding: 0;
            opacity: 0.7;
            transition: opacity 0.15s;
        }
        
        .client-tag-remove:hover {
            opacity: 1;
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
            <p class="tagline">Optimiza las rutas de entrega de Adoptamiu 🐾 Selecciona clientes de Bsale o pega links de Google Maps para obtener el orden más eficiente.</p>
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
                    <span class="card-title">Clientes de Bsale</span>
                    <button type="button" class="refresh-btn" id="refresh-clients-btn" onclick="refreshClients()" title="Actualizar clientes">
                        <span class="refresh-icon">🔄</span>
                    </button>
                </div>
                <div class="cache-status" id="cache-status"></div>
                
                <div class="input-group">
                    <label class="input-label">
                        <span class="input-label-icon">🔍</span>
                        Buscar y seleccionar clientes
                    </label>
                    <div class="client-selector">
                        <input type="text" id="client-search" class="client-search" placeholder="Escribe para buscar clientes..." autocomplete="off">
                        <div class="client-dropdown" id="client-dropdown">
                            <div class="loading-clients" id="loading-clients">
                                <span class="mini-spinner"></span>
                                Cargando clientes...
                            </div>
                        </div>
                    </div>
                    <div class="selected-clients" id="selected-clients"></div>
                    <p class="input-hint">
                        <span>💡</span>
                        Los clientes seleccionados se agregarán como paradas de entrega
                    </p>
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
            
            <button class="btn btn-primary" onclick="optimizeRoute()">
                <span>⚡</span>
                Optimizar Ruta
            </button>
            
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
                
                <div class="action-row">
                    <a href="#" class="btn btn-maps" id="maps-link" target="_blank">
                        <span>🗺️</span>
                        Abrir en Google Maps
                    </a>
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
            try {
                const response = await fetch('/api/clients', { credentials: 'same-origin' });
                const data = await response.json();
                allClients = data.clients || [];
                
                const loadingEl = document.getElementById('loading-clients');
                
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
        
        async function refreshClients() {
            const refreshBtn = document.getElementById('refresh-clients-btn');
            if (refreshBtn.classList.contains('loading')) return;
            
            refreshBtn.classList.add('loading');
            
            try {
                const response = await fetch('/api/clients/refresh', { 
                    method: 'POST',
                    credentials: 'same-origin' 
                });
                const data = await response.json();
                
                if (data.status === 'started' || data.status === 'already_loading') {
                    // Start polling for updates
                    setTimeout(loadClients, 500);
                }
            } catch (err) {
                console.error('Error refreshing clients:', err);
                refreshBtn.classList.remove('loading');
            }
        }
        
        function setupClientSearch() {
            const searchInput = document.getElementById('client-search');
            const dropdown = document.getElementById('client-dropdown');
            
            searchInput.addEventListener('focus', () => {
                dropdown.classList.add('active');
            });
            
            searchInput.addEventListener('input', (e) => {
                const query = e.target.value.toLowerCase().trim();
                if (!query) {
                    renderClientOptions(allClients);
                    return;
                }
                const filtered = allClients.filter(c => {
                    const firstName = (c.firstName || '').toLowerCase();
                    const lastName = (c.lastName || '').toLowerCase();
                    const company = (c.company || '').toLowerCase();
                    const address = (c.address || '').toLowerCase();
                    const code = (c.code || '').toLowerCase();
                    return firstName.includes(query) ||
                           lastName.includes(query) ||
                           company.includes(query) ||
                           address.includes(query) ||
                           code.includes(query);
                });
                renderClientOptions(filtered);
            });
            
            // Close dropdown when clicking outside
            document.addEventListener('click', (e) => {
                if (!e.target.closest('.client-selector')) {
                    dropdown.classList.remove('active');
                }
            });
        }
        
        function renderClientOptions(clients) {
            const dropdown = document.getElementById('client-dropdown');
            const loadingEl = document.getElementById('loading-clients');
            
            // Clear previous options (keep loading element)
            dropdown.innerHTML = '';
            
            if (clients.length === 0) {
                dropdown.innerHTML = '<div class="no-clients">No se encontraron clientes</div>';
                return;
            }
            
            clients.forEach(client => {
                const isSelected = selectedClients.some(c => c.id === client.id);
                const div = document.createElement('div');
                div.className = 'client-option' + (isSelected ? ' selected' : '');
                div.innerHTML = `
                    <div class="client-name">${client.firstName} ${client.lastName}</div>
                    <div class="client-address">${client.address}${client.municipality ? ', ' + client.municipality : ''}</div>
                    ${client.code ? '<div class="client-code">' + client.code + '</div>' : ''}
                `;
                div.onclick = () => toggleClient(client);
                dropdown.appendChild(div);
            });
        }
        
        function toggleClient(client) {
            const idx = selectedClients.findIndex(c => c.id === client.id);
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
            container.innerHTML = selectedClients.map(c => `
                <span class="client-tag">
                    ${c.firstName} ${c.lastName}
                    <button class="client-tag-remove" onclick="removeClient(${c.id})">×</button>
                </span>
            `).join('');
        }
        
        function removeClient(clientId) {
            selectedClients = selectedClients.filter(c => c.id !== clientId);
            renderSelectedClients();
            renderClientOptions(allClients);
        }
        
        async function optimizeRoute() {
            const startUrl = document.getElementById('start').value.trim();
            const endUrl = document.getElementById('end').value.trim();
            const stopsText = document.getElementById('stops').value.trim();
            
            // Get manual URL stops
            const manualStops = stopsText ? stopsText.split('\\n').filter(url => url.trim()) : [];
            
            // Get selected client IDs
            const clientIds = selectedClients.map(c => c.id);
            
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
            
            // Maps link
            document.getElementById('maps-link').href = data.google_maps_url;
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
        }
    </script>
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
    
    # Get coordinates from Bsale clients
    if client_ids:
        all_clients = fetch_bsale_clients()
        client_map = {c['id']: c for c in all_clients}
        
        for client_id in client_ids:
            client = client_map.get(client_id)
            if client:
                # Geocode the client address
                coords = geocode_address(
                    client.get('address', ''),
                    client.get('city', ''),
                    client.get('municipality', '')
                )
                if coords:
                    waypoints.append(coords)
                    waypoint_info.append({
                        'coords': coords,
                        'client_name': f"{client.get('firstName', '')} {client.get('lastName', '')}".strip(),
                        'address': client.get('address', ''),
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
            "time": "--"
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
        "google_maps_url": generate_google_maps_url(origin, destination, ordered_waypoints)
    })


if __name__ == '__main__':
    # Preload Bsale clients in background on startup
    preload_clients()
    app.run(debug=True, port=5000)
