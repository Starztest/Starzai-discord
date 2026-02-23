"""
Real astronomical calculations using Swiss Ephemeris.
Provides accurate planetary positions, houses, aspects, and transits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    import swisseph as swe
    from geopy.geocoders import Nominatim
    import pytz
    ASTRO_AVAILABLE = True
except ImportError:
    ASTRO_AVAILABLE = False
    swe = None

logger = logging.getLogger("starzai.astro")

# Planet constants
PLANETS = {
    'Sun': swe.SUN if ASTRO_AVAILABLE else 0,
    'Moon': swe.MOON if ASTRO_AVAILABLE else 1,
    'Mercury': swe.MERCURY if ASTRO_AVAILABLE else 2,
    'Venus': swe.VENUS if ASTRO_AVAILABLE else 3,
    'Mars': swe.MARS if ASTRO_AVAILABLE else 4,
    'Jupiter': swe.JUPITER if ASTRO_AVAILABLE else 5,
    'Saturn': swe.SATURN if ASTRO_AVAILABLE else 6,
    'Uranus': swe.URANUS if ASTRO_AVAILABLE else 7,
    'Neptune': swe.NEPTUNE if ASTRO_AVAILABLE else 8,
    'Pluto': swe.PLUTO if ASTRO_AVAILABLE else 9,
}

# Zodiac signs
SIGNS = [
    'Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo',
    'Libra', 'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces'
]

# Aspect definitions (name, angle, orb)
ASPECTS = [
    ('Conjunction', 0, 8),
    ('Opposition', 180, 8),
    ('Trine', 120, 8),
    ('Square', 90, 8),
    ('Sextile', 60, 6),
    ('Quincunx', 150, 3),
]


@dataclass
class Planet:
    """Represents a planet's position."""
    name: str
    longitude: float  # Absolute longitude (0-360)
    sign: str
    degree: float  # Degree within sign (0-30)
    house: int
    retrograde: bool = False

    def __str__(self) -> str:
        retro = " ℞" if self.retrograde else ""
        return f"{self.name}: {self.degree:.1f}° {self.sign}{retro} (House {self.house})"


@dataclass
class Aspect:
    """Represents an aspect between two planets."""
    planet1: str
    planet2: str
    aspect_type: str
    angle: float
    orb: float

    def __str__(self) -> str:
        return f"{self.planet1} {self.aspect_type} {self.planet2} (orb: {self.orb:.1f}°)"


@dataclass
class BirthChart:
    """Complete birth chart data."""
    planets: Dict[str, Planet]
    houses: List[float]  # House cusps
    ascendant: float
    midheaven: float
    aspects: List[Aspect]
    
    def get_chart_ruler(self) -> str:
        """Get the chart ruler based on ascendant sign."""
        asc_sign = SIGNS[int(self.ascendant / 30)]
        rulers = {
            'Aries': 'Mars', 'Taurus': 'Venus', 'Gemini': 'Mercury',
            'Cancer': 'Moon', 'Leo': 'Sun', 'Virgo': 'Mercury',
            'Libra': 'Venus', 'Scorpio': 'Pluto', 'Sagittarius': 'Jupiter',
            'Capricorn': 'Saturn', 'Aquarius': 'Uranus', 'Pisces': 'Neptune'
        }
        return rulers.get(asc_sign, 'Sun')


class AstroCalculator:
    """Calculates accurate astrological data using Swiss Ephemeris."""
    
    def __init__(self):
        if not ASTRO_AVAILABLE:
            logger.warning("Swiss Ephemeris not available - calculations disabled")
            return
        
        # Initialize geocoder
        self.geocoder = Nominatim(user_agent="starzai-discord-bot")
        
        # Set ephemeris path (uses built-in data)
        swe.set_ephe_path(None)
    
    def get_coordinates(self, location: str) -> Optional[Tuple[float, float]]:
        """Convert location name to latitude/longitude."""
        if not ASTRO_AVAILABLE:
            return None
        
        try:
            loc = self.geocoder.geocode(location, timeout=10)
            if loc:
                return (loc.latitude, loc.longitude)
            return None
        except Exception as e:
            logger.error(f"Geocoding error for '{location}': {e}")
            return None
    
    def calculate_julian_day(self, date: str, time: str, timezone: str = 'UTC') -> float:
        """Convert date/time to Julian Day."""
        if not ASTRO_AVAILABLE:
            return 0.0
        
        try:
            # Parse date and time
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            
            # Convert to UTC
            tz = pytz.timezone(timezone)
            dt_local = tz.localize(dt)
            dt_utc = dt_local.astimezone(pytz.UTC)
            
            # Calculate Julian Day
            jd = swe.julday(
                dt_utc.year, dt_utc.month, dt_utc.day,
                dt_utc.hour + dt_utc.minute / 60.0
            )
            return jd
        except Exception as e:
            logger.error(f"Julian day calculation error: {e}")
            return 0.0
    
    def get_planet_position(self, planet_id: int, jd: float) -> Tuple[float, bool]:
        """Get planet's longitude and retrograde status."""
        if not ASTRO_AVAILABLE:
            return (0.0, False)
        
        try:
            result = swe.calc_ut(jd, planet_id)
            longitude = result[0][0]  # Absolute longitude
            speed = result[0][3]  # Daily motion
            retrograde = speed < 0
            return (longitude, retrograde)
        except Exception as e:
            logger.error(f"Planet position error: {e}")
            return (0.0, False)
    
    def longitude_to_sign(self, longitude: float) -> Tuple[str, float]:
        """Convert absolute longitude to sign and degree within sign."""
        sign_index = int(longitude / 30)
        degree = longitude % 30
        return (SIGNS[sign_index], degree)
    
    def calculate_houses(self, jd: float, lat: float, lon: float) -> Tuple[List[float], float, float]:
        """Calculate house cusps and angles."""
        if not ASTRO_AVAILABLE:
            return ([0.0] * 12, 0.0, 0.0)
        
        try:
            # Calculate houses using Placidus system
            houses, ascmc = swe.houses(jd, lat, lon, b'P')
            
            # houses is a tuple of 12 house cusps
            # ascmc contains: [0]=Ascendant, [1]=MC, [2]=ARMC, [3]=Vertex
            ascendant = ascmc[0]
            midheaven = ascmc[1]
            
            return (list(houses), ascendant, midheaven)
        except Exception as e:
            logger.error(f"House calculation error: {e}")
            return ([0.0] * 12, 0.0, 0.0)
    
    def get_house_for_planet(self, planet_lon: float, house_cusps: List[float]) -> int:
        """Determine which house a planet is in."""
        if not house_cusps or len(house_cusps) < 12:
            return 1
        
        for i in range(12):
            next_i = (i + 1) % 12
            cusp = house_cusps[i]
            next_cusp = house_cusps[next_i]
            
            # Handle wrap-around at 360°
            if next_cusp < cusp:
                if planet_lon >= cusp or planet_lon < next_cusp:
                    return i + 1
            else:
                if cusp <= planet_lon < next_cusp:
                    return i + 1
        
        return 1  # Default to 1st house
    
    def calculate_aspects(self, planets: Dict[str, Planet]) -> List[Aspect]:
        """Calculate aspects between planets."""
        aspects = []
        planet_names = list(planets.keys())
        
        for i, p1_name in enumerate(planet_names):
            for p2_name in planet_names[i+1:]:
                p1 = planets[p1_name]
                p2 = planets[p2_name]
                
                # Calculate angular distance
                diff = abs(p1.longitude - p2.longitude)
                if diff > 180:
                    diff = 360 - diff
                
                # Check each aspect type
                for aspect_name, aspect_angle, max_orb in ASPECTS:
                    orb = abs(diff - aspect_angle)
                    if orb <= max_orb:
                        aspects.append(Aspect(
                            planet1=p1_name,
                            planet2=p2_name,
                            aspect_type=aspect_name,
                            angle=aspect_angle,
                            orb=orb
                        ))
                        break  # Only one aspect per planet pair
        
        return aspects
    
    def calculate_birth_chart(self, date: str, time: str, location: str) -> Optional[BirthChart]:
        """Calculate complete birth chart."""
        if not ASTRO_AVAILABLE:
            logger.error("Swiss Ephemeris not available")
            return None
        
        # Get coordinates
        coords = self.get_coordinates(location)
        if not coords:
            logger.error(f"Could not geocode location: {location}")
            return None
        
        lat, lon = coords
        
        # Calculate Julian Day (assume UTC for now, can be enhanced)
        jd = self.calculate_julian_day(date, time, 'UTC')
        
        # Calculate houses and angles
        house_cusps, ascendant, midheaven = self.calculate_houses(jd, lat, lon)
        
        # Calculate planet positions
        planets = {}
        for planet_name, planet_id in PLANETS.items():
            longitude, retrograde = self.get_planet_position(planet_id, jd)
            sign, degree = self.longitude_to_sign(longitude)
            house = self.get_house_for_planet(longitude, house_cusps)
            
            planets[planet_name] = Planet(
                name=planet_name,
                longitude=longitude,
                sign=sign,
                degree=degree,
                house=house,
                retrograde=retrograde
            )
        
        # Calculate aspects
        aspects = self.calculate_aspects(planets)
        
        return BirthChart(
            planets=planets,
            houses=house_cusps,
            ascendant=ascendant,
            midheaven=midheaven,
            aspects=aspects
        )
    
    def calculate_transits(self, natal_chart: BirthChart, transit_date: str = None) -> Dict[str, Planet]:
        """Calculate current transiting planets."""
        if not ASTRO_AVAILABLE:
            return {}
        
        # Use current date if not specified
        if transit_date is None:
            transit_date = datetime.now().strftime("%Y-%m-%d")
        
        # Calculate Julian Day for transit date
        jd = self.calculate_julian_day(transit_date, "12:00", 'UTC')
        
        # Calculate transiting planet positions
        transits = {}
        for planet_name, planet_id in PLANETS.items():
            longitude, retrograde = self.get_planet_position(planet_id, jd)
            sign, degree = self.longitude_to_sign(longitude)
            
            transits[planet_name] = Planet(
                name=planet_name,
                longitude=longitude,
                sign=sign,
                degree=degree,
                house=1,  # House relative to natal chart would need more calculation
                retrograde=retrograde
            )
        
        return transits
    
    def calculate_synastry(self, chart1: BirthChart, chart2: BirthChart) -> List[Aspect]:
        """Calculate aspects between two charts (synastry)."""
        aspects = []
        
        for p1_name, p1 in chart1.planets.items():
            for p2_name, p2 in chart2.planets.items():
                # Calculate angular distance
                diff = abs(p1.longitude - p2.longitude)
                if diff > 180:
                    diff = 360 - diff
                
                # Check each aspect type
                for aspect_name, aspect_angle, max_orb in ASPECTS:
                    orb = abs(diff - aspect_angle)
                    if orb <= max_orb:
                        aspects.append(Aspect(
                            planet1=f"Person1-{p1_name}",
                            planet2=f"Person2-{p2_name}",
                            aspect_type=aspect_name,
                            angle=aspect_angle,
                            orb=orb
                        ))
                        break
        
        return aspects
    
    def format_chart_data(self, chart: BirthChart) -> str:
        """Format birth chart data as readable text for AI."""
        lines = []
        
        # Angles
        asc_sign, asc_deg = self.longitude_to_sign(chart.ascendant)
        mc_sign, mc_deg = self.longitude_to_sign(chart.midheaven)
        lines.append(f"ASCENDANT: {asc_deg:.1f}° {asc_sign}")
        lines.append(f"MIDHEAVEN: {mc_deg:.1f}° {mc_sign}")
        lines.append(f"CHART RULER: {chart.get_chart_ruler()}")
        lines.append("")
        
        # Planets
        lines.append("PLANETARY POSITIONS:")
        for planet in chart.planets.values():
            lines.append(str(planet))
        lines.append("")
        
        # Major aspects
        lines.append("MAJOR ASPECTS:")
        major_aspects = [a for a in chart.aspects if a.aspect_type in ['Conjunction', 'Opposition', 'Trine', 'Square']]
        for aspect in sorted(major_aspects, key=lambda x: x.orb)[:15]:  # Top 15
            lines.append(str(aspect))
        
        return "\n".join(lines)

