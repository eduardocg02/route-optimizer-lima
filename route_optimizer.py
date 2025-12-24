#!/usr/bin/env python3
"""
Route Optimizer for Lima, Peru
Optimizes delivery routes using Google Routes API.
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def extract_coords_from_url(url: str) -> tuple[float, float] | None:
    """
    Extract latitude and longitude from various Google Maps URL formats.
    
    Supported formats:
    - https://maps.google.com/?q=-12.0464,-77.0428
    - https://www.google.com/maps/place/.../@-12.0464,-77.0428,17z/...
    - https://www.google.com/maps?q=-12.0464,-77.0428
    - https://goo.gl/maps/... (follows redirect)
    - https://maps.app.goo.gl/... (follows redirect)
    """
    url = url.strip()
    
    # Handle short URLs by following redirects
    if "goo.gl" in url or "maps.app" in url:
        try:
            response = requests.head(url, allow_redirects=True, timeout=10)
            url = response.url
        except requests.RequestException as e:
            print(f"Warning: Could not resolve short URL {url}: {e}")
            return None
    
    # Pattern 1: !3d and !4d format (actual place coordinates in data parameter)
    # This is the most accurate for place URLs
    place_lat = re.search(r'!3d(-?\d+\.?\d*)', url)
    place_lng = re.search(r'!4d(-?\d+\.?\d*)', url)
    if place_lat and place_lng:
        return float(place_lat.group(1)), float(place_lng.group(1))
    
    # Pattern 2: Coordinates in @ format (e.g., /@-12.0464,-77.0428,17z) - fallback
    at_pattern = r"@(-?\d+\.?\d*),(-?\d+\.?\d*)"
    match = re.search(at_pattern, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern 3: Coordinates in query parameter (e.g., ?q=-12.0464,-77.0428)
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    
    if "q" in query_params:
        q_value = query_params["q"][0]
        coord_pattern = r"(-?\d+\.?\d*),\s*(-?\d+\.?\d*)"
        match = re.search(coord_pattern, q_value)
        if match:
            return float(match.group(1)), float(match.group(2))
    
    # Pattern 4: Coordinates in the path for place URLs
    # e.g., /maps/place/Some+Place/-12.0464,-77.0428
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
    
    print(f"Warning: Could not extract coordinates from URL: {url}")
    return None


def optimize_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
    waypoints: list[tuple[float, float]],
) -> dict | None:
    """
    Use Google Routes API to compute the optimal route order.
    
    Args:
        origin: (lat, lng) of start point
        destination: (lat, lng) of end point
        waypoints: List of (lat, lng) tuples for intermediate stops
    
    Returns:
        API response with optimized route or None on error
    """
    if not GOOGLE_API_KEY:
        print("Error: GOOGLE_MAPS_API_KEY not set in environment")
        sys.exit(1)
    
    def make_waypoint(coords: tuple[float, float]) -> dict:
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
        "X-Goog-FieldMask": "routes.optimizedIntermediateWaypointIndex,routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters,routes.legs.startLocation,routes.legs.endLocation",
    }
    
    try:
        response = requests.post(
            ROUTES_API_URL,
            json=request_body,
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error calling Routes API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        return None


def generate_google_maps_url(
    origin: tuple[float, float],
    destination: tuple[float, float],
    ordered_waypoints: list[tuple[float, float]],
) -> str:
    """Generate a Google Maps directions URL with waypoints in order."""
    base_url = "https://www.google.com/maps/dir/"
    
    # Build the path: origin / waypoints / destination
    points = [origin] + ordered_waypoints + [destination]
    path_parts = [f"{lat},{lng}" for lat, lng in points]
    
    return base_url + "/".join(path_parts)


def format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"


def format_distance(meters: int) -> str:
    """Format meters into human-readable distance."""
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{meters} m"


def main():
    parser = argparse.ArgumentParser(
        description="Optimize delivery routes in Lima, Peru using Google Routes API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # With individual URLs
  python route_optimizer.py --start "https://maps.google.com/?q=-12.0464,-77.0428" \\
                            --end "https://maps.google.com/?q=-12.1,-77.05" \\
                            --stops "url1" "url2" "url3"

  # From a file (one URL per line)
  python route_optimizer.py --start "url" --end "url" --file stops.txt
        """
    )
    
    parser.add_argument(
        "--start", "-s",
        required=True,
        help="Google Maps URL for the starting point"
    )
    parser.add_argument(
        "--end", "-e",
        required=True,
        help="Google Maps URL for the ending point"
    )
    parser.add_argument(
        "--stops",
        nargs="*",
        default=[],
        help="Google Maps URLs for intermediate stops"
    )
    parser.add_argument(
        "--file", "-f",
        help="File containing Google Maps URLs (one per line)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format"
    )
    
    args = parser.parse_args()
    
    # Collect all stop URLs
    stop_urls = list(args.stops) if args.stops else []
    
    if args.file:
        try:
            with open(args.file, "r") as f:
                file_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                stop_urls.extend(file_urls)
        except FileNotFoundError:
            print(f"Error: File not found: {args.file}")
            sys.exit(1)
    
    if not stop_urls:
        print("Error: No stops provided. Use --stops or --file")
        sys.exit(1)
    
    # Extract coordinates
    print("Parsing URLs...")
    
    origin = extract_coords_from_url(args.start)
    if not origin:
        print(f"Error: Could not parse start URL: {args.start}")
        sys.exit(1)
    
    destination = extract_coords_from_url(args.end)
    if not destination:
        print(f"Error: Could not parse end URL: {args.end}")
        sys.exit(1)
    
    waypoints = []
    original_urls = []
    for url in stop_urls:
        coords = extract_coords_from_url(url)
        if coords:
            waypoints.append(coords)
            original_urls.append(url)
        else:
            print(f"Skipping invalid URL: {url}")
    
    if not waypoints:
        print("Error: No valid waypoints found")
        sys.exit(1)
    
    print(f"Found {len(waypoints)} valid stops")
    print(f"Origin: {origin}")
    print(f"Destination: {destination}")
    print()
    
    # Optimize route
    print("Optimizing route...")
    result = optimize_route(origin, destination, waypoints)
    
    if not result or "routes" not in result:
        print("Error: Could not optimize route")
        if result:
            print(f"API Response: {json.dumps(result, indent=2)}")
        sys.exit(1)
    
    route = result["routes"][0]
    optimized_order = route.get("optimizedIntermediateWaypointIndex", list(range(len(waypoints))))
    
    # Reorder waypoints
    ordered_waypoints = [waypoints[i] for i in optimized_order]
    ordered_urls = [original_urls[i] for i in optimized_order]
    
    # Calculate totals
    total_distance = route.get("distanceMeters", 0)
    total_duration_str = route.get("duration", "0s")
    total_duration = int(total_duration_str.rstrip("s"))
    
    if args.json:
        output = {
            "origin": {"coords": origin, "url": args.start},
            "destination": {"coords": destination, "url": args.end},
            "optimized_stops": [
                {"index": i + 1, "coords": coords, "original_url": url}
                for i, (coords, url) in enumerate(zip(ordered_waypoints, ordered_urls))
            ],
            "total_distance_meters": total_distance,
            "total_duration_seconds": total_duration,
            "google_maps_url": generate_google_maps_url(origin, destination, ordered_waypoints),
        }
        print(json.dumps(output, indent=2))
    else:
        print("=" * 60)
        print("OPTIMIZED ROUTE")
        print("=" * 60)
        print()
        print(f"START: {origin[0]:.6f}, {origin[1]:.6f}")
        print()
        
        legs = route.get("legs", [])
        for i, (coords, url) in enumerate(zip(ordered_waypoints, ordered_urls)):
            print(f"  Stop {i + 1}: {coords[0]:.6f}, {coords[1]:.6f}")
            if i < len(legs):
                leg = legs[i]
                leg_dist = leg.get("distanceMeters", 0)
                leg_dur_str = leg.get("duration", "0s")
                leg_dur = int(leg_dur_str.rstrip("s"))
                print(f"           Distance: {format_distance(leg_dist)}, Time: {format_duration(leg_dur)}")
            print()
        
        print(f"END: {destination[0]:.6f}, {destination[1]:.6f}")
        if legs:
            last_leg = legs[-1]
            leg_dist = last_leg.get("distanceMeters", 0)
            leg_dur_str = last_leg.get("duration", "0s")
            leg_dur = int(leg_dur_str.rstrip("s"))
            print(f"     Distance: {format_distance(leg_dist)}, Time: {format_duration(leg_dur)}")
        print()
        print("-" * 60)
        print(f"TOTAL DISTANCE: {format_distance(total_distance)}")
        print(f"TOTAL TIME: {format_duration(total_duration)}")
        print("-" * 60)
        print()
        print("Google Maps URL:")
        print(generate_google_maps_url(origin, destination, ordered_waypoints))


if __name__ == "__main__":
    main()

