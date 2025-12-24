#!/usr/bin/env python3
"""
Route Optimizer Web Interface
A simple Flask app for testing the route optimizer.
"""

import json
import os
import re
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify

load_dotenv()

app = Flask(__name__)

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


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
    # This is the most accurate for place URLs
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
            # Get the first (most specific) result
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


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Route Optimizer - Lima</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-card: #1a1a24;
            --accent: #00ff88;
            --accent-dim: #00cc6a;
            --text-primary: #f0f0f5;
            --text-secondary: #8888aa;
            --border: #2a2a3a;
            --error: #ff4466;
            --warning: #ffaa00;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            background-image: 
                radial-gradient(ellipse at 20% 0%, rgba(0, 255, 136, 0.08) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 100%, rgba(0, 200, 100, 0.05) 0%, transparent 50%);
        }
        
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        
        header {
            text-align: center;
            margin-bottom: 50px;
        }
        
        h1 {
            font-family: 'Space Mono', monospace;
            font-size: 2.5rem;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: -1px;
            margin-bottom: 8px;
        }
        
        .subtitle {
            color: var(--text-secondary);
            font-size: 1.1rem;
            font-weight: 300;
        }
        
        .card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 32px;
            margin-bottom: 24px;
        }
        
        .card-title {
            font-family: 'Space Mono', monospace;
            font-size: 0.85rem;
            color: var(--accent);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 20px;
        }
        
        .input-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            font-size: 0.9rem;
            color: var(--text-secondary);
            margin-bottom: 8px;
        }
        
        input, textarea {
            width: 100%;
            padding: 14px 16px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text-primary);
            font-family: 'Space Mono', monospace;
            font-size: 0.9rem;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        
        input:focus, textarea:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(0, 255, 136, 0.1);
        }
        
        textarea {
            min-height: 150px;
            resize: vertical;
            line-height: 1.6;
        }
        
        .hint {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-top: 6px;
            opacity: 0.7;
        }
        
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 16px 32px;
            font-family: 'Space Mono', monospace;
            font-size: 1rem;
            font-weight: 700;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .btn-primary {
            background: var(--accent);
            color: var(--bg-primary);
            width: 100%;
        }
        
        .btn-primary:hover {
            background: var(--accent-dim);
            transform: translateY(-2px);
            box-shadow: 0 8px 30px rgba(0, 255, 136, 0.3);
        }
        
        .btn-primary:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .btn-secondary {
            background: transparent;
            color: var(--accent);
            border: 1px solid var(--accent);
        }
        
        .btn-secondary:hover {
            background: rgba(0, 255, 136, 0.1);
        }
        
        #results {
            display: none;
        }
        
        .route-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            margin-bottom: 30px;
        }
        
        .route-stats {
            display: flex;
            gap: 30px;
        }
        
        .stat {
            text-align: center;
        }
        
        .stat-value {
            font-family: 'Space Mono', monospace;
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--accent);
        }
        
        .stat-label {
            font-size: 0.8rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .route-timeline {
            position: relative;
            padding-left: 30px;
        }
        
        .route-timeline::before {
            content: '';
            position: absolute;
            left: 8px;
            top: 10px;
            bottom: 10px;
            width: 2px;
            background: linear-gradient(to bottom, var(--accent), var(--accent-dim));
        }
        
        .route-stop {
            position: relative;
            padding: 16px 0;
            border-bottom: 1px solid var(--border);
        }
        
        .route-stop:last-child {
            border-bottom: none;
        }
        
        .route-stop::before {
            content: '';
            position: absolute;
            left: -26px;
            top: 22px;
            width: 14px;
            height: 14px;
            background: var(--bg-card);
            border: 3px solid var(--accent);
            border-radius: 50%;
        }
        
        .route-stop.start::before,
        .route-stop.end::before {
            background: var(--accent);
        }
        
        .stop-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }
        
        .stop-label {
            font-weight: 600;
            font-size: 1rem;
        }
        
        .stop-label.start, .stop-label.end {
            color: var(--accent);
        }
        
        .stop-metrics {
            font-family: 'Space Mono', monospace;
            font-size: 0.85rem;
            color: var(--text-secondary);
        }
        
        .stop-address {
            font-size: 0.95rem;
            color: var(--text-primary);
            margin-bottom: 4px;
            line-height: 1.4;
        }
        
        .stop-coords {
            font-family: 'Space Mono', monospace;
            font-size: 0.75rem;
            color: var(--text-secondary);
            opacity: 0.5;
        }
        
        .maps-link {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-top: 24px;
            padding: 14px 24px;
            background: linear-gradient(135deg, #4285f4, #34a853);
            color: white;
            text-decoration: none;
            border-radius: 10px;
            font-weight: 600;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .maps-link:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 30px rgba(66, 133, 244, 0.4);
        }
        
        .error-message {
            background: rgba(255, 68, 102, 0.1);
            border: 1px solid var(--error);
            border-radius: 10px;
            padding: 16px 20px;
            color: var(--error);
            font-family: 'Space Mono', monospace;
            font-size: 0.9rem;
        }
        
        .loading {
            display: none;
            text-align: center;
            padding: 40px;
        }
        
        .loading.active {
            display: block;
        }
        
        .spinner {
            width: 50px;
            height: 50px;
            border: 3px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .action-buttons {
            display: flex;
            gap: 12px;
            margin-top: 24px;
        }
        
        .action-buttons .btn {
            flex: 1;
        }
        
        @media (max-width: 600px) {
            h1 {
                font-size: 1.8rem;
            }
            
            .route-stats {
                flex-direction: column;
                gap: 16px;
            }
            
            .action-buttons {
                flex-direction: column;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>// ROUTE_OPTIMIZER</h1>
            <p class="subtitle">Optimiza rutas de entrega en Lima usando Google Routes API</p>
        </header>
        
        <div id="form-section">
            <div class="card">
                <div class="card-title">Puntos de inicio y fin</div>
                
                <div class="input-group">
                    <label for="start">URL de Google Maps - Inicio</label>
                    <input type="text" id="start" placeholder="https://www.google.com/maps/place/.../@-12.0464,-77.0428,17z">
                </div>
                
                <div class="input-group">
                    <label for="end">URL de Google Maps - Fin</label>
                    <input type="text" id="end" placeholder="https://www.google.com/maps/place/.../@-12.1,-77.05,17z">
                </div>
            </div>
            
            <div class="card">
                <div class="card-title">Paradas intermedias</div>
                
                <div class="input-group">
                    <label for="stops">URLs de Google Maps (una por línea)</label>
                    <textarea id="stops" placeholder="https://www.google.com/maps/place/.../@-12.08,-77.03,17z
https://www.google.com/maps/place/.../@-12.09,-77.04,17z
https://www.google.com/maps/place/.../@-12.10,-77.02,17z"></textarea>
                    <p class="hint">Pega las URLs de Google Maps, una por línea. El sistema optimizará el orden automáticamente.</p>
                </div>
            </div>
            
            <button class="btn btn-primary" id="optimize-btn" onclick="optimizeRoute()">
                <span>⚡</span> Optimizar Ruta
            </button>
        </div>
        
        <div class="loading" id="loading">
            <div class="spinner"></div>
            <p>Calculando la ruta óptima...</p>
        </div>
        
        <div id="results">
            <div class="card">
                <div class="route-header">
                    <div class="card-title">Ruta Optimizada</div>
                    <div class="route-stats">
                        <div class="stat">
                            <div class="stat-value" id="total-distance">--</div>
                            <div class="stat-label">Distancia</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value" id="total-time">--</div>
                            <div class="stat-label">Tiempo</div>
                        </div>
                    </div>
                </div>
                
                <div class="route-timeline" id="route-timeline">
                    <!-- Populated by JS -->
                </div>
                
                <a href="#" class="maps-link" id="maps-link" target="_blank">
                    <span>🗺️</span> Abrir en Google Maps
                </a>
                
                <div class="action-buttons">
                    <button class="btn btn-secondary" onclick="resetForm()">
                        ← Nueva Ruta
                    </button>
                </div>
            </div>
        </div>
        
        <div id="error-container"></div>
    </div>

    <script>
        async function optimizeRoute() {
            const startUrl = document.getElementById('start').value.trim();
            const endUrl = document.getElementById('end').value.trim();
            const stopsText = document.getElementById('stops').value.trim();
            
            if (!startUrl || !endUrl || !stopsText) {
                showError('Por favor completa todos los campos');
                return;
            }
            
            const stops = stopsText.split('\\n').filter(url => url.trim());
            
            if (stops.length === 0) {
                showError('Agrega al menos una parada intermedia');
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
                    body: JSON.stringify({
                        start: startUrl,
                        end: endUrl,
                        stops: stops
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
                <div class="route-stop start">
                    <div class="stop-header">
                        <span class="stop-label start">INICIO</span>
                    </div>
                    <div class="stop-address">${data.origin_address}</div>
                    <div class="stop-coords">${data.origin[0].toFixed(6)}, ${data.origin[1].toFixed(6)}</div>
                </div>
            `;
            
            // Stops
            data.stops.forEach((stop, i) => {
                timeline.innerHTML += `
                    <div class="route-stop">
                        <div class="stop-header">
                            <span class="stop-label">Parada ${i + 1}</span>
                            <span class="stop-metrics">${stop.distance} · ${stop.time}</span>
                        </div>
                        <div class="stop-address">${stop.address}</div>
                        <div class="stop-coords">${stop.coords[0].toFixed(6)}, ${stop.coords[1].toFixed(6)}</div>
                    </div>
                `;
            });
            
            // End
            timeline.innerHTML += `
                <div class="route-stop end">
                    <div class="stop-header">
                        <span class="stop-label end">FIN</span>
                        <span class="stop-metrics">${data.last_leg_distance} · ${data.last_leg_time}</span>
                    </div>
                    <div class="stop-address">${data.destination_address}</div>
                    <div class="stop-coords">${data.destination[0].toFixed(6)}, ${data.destination[1].toFixed(6)}</div>
                </div>
            `;
            
            // Maps link
            document.getElementById('maps-link').href = data.google_maps_url;
        }
        
        function showError(message) {
            document.getElementById('error-container').innerHTML = `
                <div class="error-message">${message}</div>
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
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/optimize', methods=['POST'])
def optimize():
    data = request.json
    
    start_url = data.get('start', '')
    end_url = data.get('end', '')
    stop_urls = data.get('stops', [])
    
    # Parse coordinates
    origin = extract_coords_from_url(start_url)
    if not origin:
        return jsonify({"error": f"No se pudo extraer coordenadas del inicio: {start_url}"})
    
    destination = extract_coords_from_url(end_url)
    if not destination:
        return jsonify({"error": f"No se pudo extraer coordenadas del fin: {end_url}"})
    
    waypoints = []
    for url in stop_urls:
        coords = extract_coords_from_url(url)
        if coords:
            waypoints.append(coords)
    
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
    
    # Reorder waypoints
    ordered_waypoints = [waypoints[i] for i in optimized_order]
    
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
    for i, coords in enumerate(ordered_waypoints):
        leg_info = {
            "coords": coords,
            "address": reverse_geocode(coords),
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
    app.run(debug=True, port=5000)

