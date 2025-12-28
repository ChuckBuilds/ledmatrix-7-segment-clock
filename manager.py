"""
7-Segment Clock Plugin for LEDMatrix

Displays a retro-style 7-segment clock with configurable time formats
and sunrise/sunset-based color transitions.
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import pytz
from PIL import Image, ImageDraw
from astral import LocationInfo
from astral.sun import elevation
from astral import Observer

from src.plugin_system.base_plugin import BasePlugin


class SevenSegmentClockPlugin(BasePlugin):
    """7-segment clock plugin with sunrise/sunset color transitions."""

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        display_manager: Any,
        cache_manager: Any,
        plugin_manager: Any,
    ) -> None:
        """Initialize the 7-segment clock plugin."""
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)

        # Get plugin directory for asset loading
        self.plugin_dir = Path(__file__).parent
        self.assets_dir = self.plugin_dir / "assets" / "images"

        # Load configuration
        self.location_config = config.get("location", {})
        self.is_24_hour_format = config.get("is_24_hour_format", True)
        self.has_leading_zero = config.get("has_leading_zero", False)
        self.has_flashing_separator = config.get("has_flashing_separator", True)
        self.color_daytime = self._hex_to_rgb(config.get("color_daytime", "#FFFFFF"))
        self.color_nighttime = self._hex_to_rgb(config.get("color_nighttime", "#FFFFFF"))
        self.min_fade_elevation = int(config.get("min_fade_elevation", "-1"))

        # Initialize location and timezone
        self._init_location()
        self._init_timezone()

        # Load digit and separator images
        self.number_images = self._load_number_images()
        self.separator_image = self._load_separator_image()

        # State variables (updated in update(), used in display())
        self.current_time: Optional[datetime] = None
        self.current_color: Tuple[int, int, int] = self.color_daytime
        self.sun_elevation: float = 0.0

        # Image dimensions (from loaded images)
        self.digit_width = 13
        self.digit_height = 32
        self.separator_width = 4
        self.separator_height = 14

        self.logger.info("7-segment clock plugin initialized")

    def _init_location(self) -> None:
        """Initialize location for sunrise/sunset calculations."""
        lat = self.location_config.get("lat", 37.541290)
        lng = self.location_config.get("lng", -77.434769)
        locality = self.location_config.get("locality", "Richmond, VA")

        self.location = LocationInfo(
            name=locality,
            region="",
            timezone="UTC",  # Timezone handled separately
            latitude=lat,
            longitude=lng,
        )
        self.observer = Observer(lat, lng)

    def _init_timezone(self) -> None:
        """Initialize timezone from config or system default."""
        timezone_str = self.location_config.get("timezone", "US/Eastern")
        try:
            self.timezone = pytz.timezone(timezone_str)
        except pytz.exceptions.UnknownTimeZoneError:
            self.logger.warning(f"Unknown timezone '{timezone_str}', using UTC")
            self.timezone = pytz.UTC

    def _load_number_images(self) -> Dict[int, Image.Image]:
        """Load all number digit images (0-9)."""
        images = {}
        for i in range(10):
            image_path = self.assets_dir / f"number_{i}.png"
            try:
                if image_path.exists():
                    images[i] = Image.open(image_path).convert("RGBA")
                    self.logger.debug(f"Loaded number image: {i}")
                else:
                    self.logger.warning(f"Number image not found: {image_path}")
            except Exception as e:
                self.logger.error(f"Error loading number image {i}: {e}")
        
        if len(images) != 10:
            self.logger.error(f"Only loaded {len(images)}/10 number images")
        
        return images

    def _load_separator_image(self) -> Optional[Image.Image]:
        """Load the separator (colon) image."""
        image_path = self.assets_dir / "separator.png"
        try:
            if image_path.exists():
                image = Image.open(image_path).convert("RGBA")
                self.logger.debug("Loaded separator image")
                return image
            else:
                self.logger.warning(f"Separator image not found: {image_path}")
                return None
        except Exception as e:
            self.logger.error(f"Error loading separator image: {e}")
            return None

    def _hex_to_rgb(self, hex_color: str) -> Tuple[int, int, int]:
        """Convert hex color string to RGB tuple."""
        # Remove # if present
        hex_color = hex_color.lstrip("#")
        
        # Handle 3-digit hex (e.g., #FFF -> #FFFFFF)
        if len(hex_color) == 3:
            hex_color = "".join(c * 2 for c in hex_color)
        
        # Convert to RGB
        try:
            return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            self.logger.warning(f"Invalid hex color '{hex_color}', using white")
            return (255, 255, 255)

    def _rgb_to_hex(self, r: int, g: int, b: int) -> str:
        """Convert RGB tuple to hex color string."""
        return f"#{r:02x}{g:02x}{b:02x}"

    def _mix_colors(
        self, color1: Tuple[int, int, int], color2: Tuple[int, int, int], percentage: float
    ) -> Tuple[int, int, int]:
        """
        Mix two colors by a given percentage.

        Args:
            color1: First color (RGB tuple)
            color2: Second color (RGB tuple)
            percentage: Percentage of color1 in mix (0.0-1.0)

        Returns:
            Mixed color as RGB tuple
        """
        r1, g1, b1 = color1
        r2, g2, b2 = color2

        r = int(r1 * percentage + r2 * (1 - percentage))
        g = int(g1 * percentage + g2 * (1 - percentage))
        b = int(b1 * percentage + b2 * (1 - percentage))

        return (r, g, b)

    def _proportion_within_range(self, min_value: float, max_value: float, x: float) -> float:
        """
        Calculate the proportion of x within the range [min_value, max_value],
        clamped between 0 and 1.

        Args:
            min_value: Minimum value of the range
            max_value: Maximum value of the range
            x: Value to calculate proportion for

        Returns:
            Proportion between 0 and 1
        """
        if min_value == max_value:
            return float(x >= min_value)

        proportion = (x - min_value) / (max_value - min_value)
        return max(0.0, min(1.0, proportion))

    def _get_sun_elevation(self, dt: datetime) -> float:
        """
        Calculate sun elevation at given datetime.

        Args:
            dt: Datetime object (should be timezone-aware)

        Returns:
            Sun elevation in degrees
        """
        try:
            # Convert to UTC for astral calculations
            if dt.tzinfo is None:
                dt = self.timezone.localize(dt)
            dt_utc = dt.astimezone(pytz.UTC)
            
            elev = elevation(self.observer, dt_utc)
            return elev
        except Exception as e:
            self.logger.error(f"Error calculating sun elevation: {e}")
            return 0.0

    def _format_time(self, dt: datetime) -> Tuple[str, bool]:
        """
        Format time string based on configuration.

        Args:
            dt: Datetime object

        Returns:
            Tuple of (time_string, separator_visible)
            - time_string: Formatted time (e.g., "12:34" or "09:05")
            - separator_visible: Whether separator should be visible (for flashing)
        """
        if self.is_24_hour_format:
            hour_str = dt.strftime("%H")
        else:
            hour_str = dt.strftime("%I")  # 12-hour format with leading zero
            if not self.has_leading_zero and hour_str[0] == "0":
                hour_str = hour_str[1:]  # Remove leading zero
        
        if not self.has_leading_zero and self.is_24_hour_format and hour_str[0] == "0":
            hour_str = hour_str[1:]  # Remove leading zero for 24-hour format too

        minute_str = dt.strftime("%M")

        # Determine separator visibility for flashing
        separator_visible = True
        if self.has_flashing_separator:
            # Flash on even seconds (separator visible on 0, 2, 4, etc.)
            separator_visible = (dt.second % 2) == 0

        return f"{hour_str}:{minute_str}", separator_visible

    def _render_digit(
        self, digit: int, color: Tuple[int, int, int]
    ) -> Optional[Image.Image]:
        """
        Render a single digit with the specified color.

        Args:
            digit: Digit to render (0-9)
            color: RGB color tuple

        Returns:
            PIL Image with colored digit, or None if error
        """
        if digit not in self.number_images:
            self.logger.warning(f"Digit image not available: {digit}")
            return None

        # Get the base image (transparent foreground on black background)
        base_image = self.number_images[digit].copy()

        # Create a colored version
        # The TronbyT images have transparent pixels for the "lit" segments
        # We need to replace non-transparent, non-black pixels with the desired color
        colored_image = Image.new("RGBA", base_image.size, (0, 0, 0, 255))
        
        # Apply color to visible pixels (non-transparent, non-black)
        for x in range(base_image.width):
            for y in range(base_image.height):
                pixel = base_image.getpixel((x, y))
                if len(pixel) == 4:  # RGBA
                    r, g, b, a = pixel
                    # If pixel has alpha and is not black, apply the color
                    if a > 0 and (r, g, b) != (0, 0, 0):
                        # Apply color while preserving alpha
                        colored_image.putpixel((x, y), (*color, a))
                    else:
                        # Keep black/transparent pixels as-is
                        colored_image.putpixel((x, y), pixel)
                else:
                    colored_image.putpixel((x, y), pixel)

        return colored_image

    def _render_separator(
        self, color: Tuple[int, int, int]
    ) -> Optional[Image.Image]:
        """
        Render the separator (colon) with the specified color.

        Args:
            color: RGB color tuple

        Returns:
            PIL Image with colored separator, or None if error
        """
        if self.separator_image is None:
            return None

        # Similar to digit rendering
        base_image = self.separator_image.copy()
        colored_image = Image.new("RGBA", base_image.size, (0, 0, 0, 255))

        for x in range(base_image.width):
            for y in range(base_image.height):
                pixel = base_image.getpixel((x, y))
                if len(pixel) == 4:  # RGBA
                    r, g, b, a = pixel
                    # If pixel has alpha and is not black, apply the color
                    if a > 0 and (r, g, b) != (0, 0, 0):
                        colored_image.putpixel((x, y), (*color, a))
                    else:
                        # Keep black/transparent pixels as-is
                        colored_image.putpixel((x, y), pixel)
                else:
                    colored_image.putpixel((x, y), pixel)

        return colored_image

    def update(self) -> None:
        """Update current time, sun elevation, and target color."""
        try:
            # Get current time in configured timezone
            now_utc = datetime.now(pytz.UTC)
            self.current_time = now_utc.astimezone(self.timezone)

            # Calculate sun elevation
            self.sun_elevation = self._get_sun_elevation(self.current_time)

            # Calculate color based on sun elevation
            # Mix between daytime and nighttime colors
            max_elevation = -1.0  # Default max elevation
            min_elevation = float(self.min_fade_elevation)

            if min_elevation == max_elevation:
                # No fading - use daytime color if sun is up, nighttime if down
                if self.sun_elevation >= max_elevation:
                    self.current_color = self.color_daytime
                else:
                    self.current_color = self.color_nighttime
            else:
                # Fade between colors based on elevation
                proportion = self._proportion_within_range(
                    min_elevation, max_elevation, self.sun_elevation
                )
                self.current_color = self._mix_colors(
                    self.color_daytime, self.color_nighttime, proportion
                )

            self.logger.debug(
                f"Updated: time={self.current_time.strftime('%H:%M:%S')}, "
                f"elevation={self.sun_elevation:.1f}°, color={self.current_color}"
            )

        except Exception as e:
            self.logger.error(f"Error in update(): {e}", exc_info=True)
            # Fallback to current time and default color
            self.current_time = datetime.now(self.timezone)
            self.current_color = self.color_daytime

    def display(self, force_clear: bool = False) -> None:
        """Render the 7-segment clock display."""
        try:
            if force_clear:
                self.display_manager.clear()

            # Use cached time and color from update()
            if self.current_time is None:
                self.update()

            # Format time string
            time_str, separator_visible = self._format_time(self.current_time)

            # Get display dimensions
            display_width = self.display_manager.width
            display_height = self.display_manager.height

            # Calculate total width of time display
            digits = []
            for char in time_str:
                if char == ":":
                    if separator_visible and self.separator_image:
                        digits.append(":")
                    else:
                        digits.append(None)  # Skip separator
                elif char.isdigit():
                    digits.append(int(char))

            # Calculate width: each digit is digit_width, separator is separator_width
            total_width = 0
            for item in digits:
                if item == ":":
                    total_width += self.separator_width
                elif item is not None:
                    total_width += self.digit_width

            # Calculate starting X position to center the display
            start_x = (display_width - total_width) // 2
            # Center vertically
            start_y = (display_height - self.digit_height) // 2

            # Render each digit/separator
            current_x = start_x
            for item in digits:
                if item == ":":
                    # Render separator
                    sep_img = self._render_separator(self.current_color)
                    if sep_img:
                        # Paste onto display image
                        paste_y = start_y + (self.digit_height - self.separator_height) // 2
                        self.display_manager.image.paste(
                            sep_img, (current_x, paste_y), sep_img
                        )
                        current_x += self.separator_width
                elif item is not None:
                    # Render digit
                    digit_img = self._render_digit(item, self.current_color)
                    if digit_img:
                        self.display_manager.image.paste(
                            digit_img, (current_x, start_y), digit_img
                        )
                        current_x += self.digit_width

            # Update the display
            self.display_manager.update_display()

        except Exception as e:
            self.logger.error(f"Error in display(): {e}", exc_info=True)
            # Show error on display
            self.display_manager.clear()
            self.display_manager.draw_text(
                "Clock Error",
                x=10,
                y=10,
                color=(255, 0, 0)
            )
            self.display_manager.update_display()

    def validate_config(self) -> bool:
        """Validate plugin configuration."""
        # Check location
        location = self.config.get("location", {})
        if not isinstance(location, dict):
            self.logger.error("Location must be an object")
            return False

        lat = location.get("lat")
        lng = location.get("lng")
        if lat is not None and (not isinstance(lat, (int, float)) or lat < -90 or lat > 90):
            self.logger.error("Latitude must be between -90 and 90")
            return False
        if lng is not None and (not isinstance(lng, (int, float)) or lng < -180 or lng > 180):
            self.logger.error("Longitude must be between -180 and 180")
            return False

        # Check colors
        for color_key in ["color_daytime", "color_nighttime"]:
            color = self.config.get(color_key, "#FFFFFF")
            if not isinstance(color, str) or not color.startswith("#"):
                self.logger.warning(f"{color_key} should be a hex color (e.g., #FFFFFF)")

        return True

