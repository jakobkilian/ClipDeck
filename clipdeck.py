import sys
import threading
import time
import os
import json
import math
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
import colorsys
from pythonosc import dispatcher, osc_server, udp_client


# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "deck_config.json")
FLASH_COLOR = (170, 170, 170)  
FLASH_DURATION = 0.2  # seconds


# Scroll mode constants
SCROLL_NONE = "none"
SCROLL_VERTICAL = "vertical"
SCROLL_BOTH = "both"
SCROLL_BOTH_RESET = "both-reset"

# Keys for scroll overlays
KEY_BURGER = 31        # Menu button (lower right)
KEY_BRIGHTNESS = 23    # Brightness button (above burger)
KEY_ARROW_RIGHT = 30   # Right of burger
KEY_ARROW_DOWN = 29    # Left of right
KEY_ARROW_UP = 21      # Above down
KEY_ARROW_LEFT = 28    # Left of down
SCROLL_KEYS_VERTICAL = {30, 31}  # Up/down arrows
SCROLL_KEYS_BOTH = {KEY_BURGER, KEY_BRIGHTNESS, KEY_ARROW_RIGHT, KEY_ARROW_DOWN, KEY_ARROW_UP, KEY_ARROW_LEFT}

# Debug mode (not saved to config, set via command line)
debug_mode = False


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            # Migrate old vertical_scrolling to scroll_mode
            if "vertical_scrolling" in config and "scroll_mode" not in config:
                config["scroll_mode"] = SCROLL_VERTICAL if config["vertical_scrolling"] else SCROLL_NONE
            return config
    except:
        return {"decks": {}, "scroll_mode": SCROLL_VERTICAL}


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def light_deck(deck, on=True):
    """Light up all keys on a deck white (or turn off)."""
    try:
        for key in range(deck.key_count()):
            image = PILHelper.create_image(deck)
            draw = ImageDraw.Draw(image)
            color = (255, 255, 255) if on else (0, 0, 0)
            draw.rectangle([(0, 0), (image.width, image.height)], fill=color)
            native_image = PILHelper.to_native_key_format(deck, image.convert("RGB"))
            with deck:
                deck.set_key_image(key, native_image)
    except:
        pass


def interactive_setup(discovered_decks, force_menu=False):
    """Interactive configuration wizard for StreamDecks."""
    global debug_mode
    
    config = load_config()
    saved_decks = config.get("decks", {})
    saved_scroll = config.get("scroll_mode", SCROLL_VERTICAL)
    saved_brightness = config.get("brightness", 3)
    current_serials = {d.get_serial_number() for d in discovered_decks}
    saved_serials = set(saved_decks.keys())
    
    # If not forcing menu, try to use saved config silently
    if not force_menu:
        if saved_serials and saved_serials.issubset(current_serials) and saved_decks:
            print("Loading saved configuration...")
            return config
        else:
            print("No valid saved configuration found, entering setup...")
    
    # --- Interactive Menu ---
    print("\n" + "=" * 60)
    print("ClipDeck Configuration")
    print("Press ENTER to keep current/default value for any setting.")
    print("=" * 60)
    
    # Debug mode (not saved)
    debug_input = input("\nDebug mode? (y/n) [default=n]: ").strip().lower()
    debug_mode = (debug_input == 'y')
    if debug_mode:
        print("  -> Debug mode ENABLED")
    
    # Start with saved config as base
    has_saved = saved_serials and saved_serials.issubset(current_serials) and saved_decks
    new_config = {
        "decks": saved_decks.copy() if has_saved else {},
        "scroll_mode": saved_scroll,
        "brightness": saved_brightness
    }
    
    # --- Brightness (always asked) ---
    print("\n--- Brightness ---")
    while True:
        bright_input = input(f"Brightness [1-10, current={saved_brightness}]: ").strip()
        if bright_input == "":
            new_config["brightness"] = saved_brightness
            print(f"  -> Keeping brightness: {saved_brightness}")
            break
        try:
            bright_val = int(bright_input)
            if 1 <= bright_val <= 10:
                new_config["brightness"] = bright_val
                print(f"  -> Brightness: {bright_val}")
                break
            else:
                print("  ! Please enter 1-10")
        except ValueError:
            print("  ! Please enter a valid number")
    
    # --- Scroll Mode (always asked) ---
    print("\n--- Scroll Mode ---")
    scroll_map = {SCROLL_NONE: "1", SCROLL_VERTICAL: "2", SCROLL_BOTH: "3", SCROLL_BOTH_RESET: "4"}
    current_scroll_num = scroll_map.get(saved_scroll, "2")
    print("  1. none       - No scrolling buttons")
    print("  2. vertical   - Up/Down arrows (bottom right)")
    print("  3. both       - Menu button with 4-way arrows")
    print("  4. both-reset - Like both, but left arrow resets to original position")
    while True:
        scroll_input = input(f"Select scroll mode [1-4, current={current_scroll_num}]: ").strip()
        if scroll_input == "":
            new_config["scroll_mode"] = saved_scroll
            print(f"  -> Keeping: {saved_scroll}")
            break
        elif scroll_input == "1":
            new_config["scroll_mode"] = SCROLL_NONE
            print("  -> No scrolling")
            break
        elif scroll_input == "2":
            new_config["scroll_mode"] = SCROLL_VERTICAL
            print("  -> Vertical scrolling")
            break
        elif scroll_input == "3":
            new_config["scroll_mode"] = SCROLL_BOTH
            print("  -> Both directions (menu button)")
            break
        elif scroll_input == "4":
            new_config["scroll_mode"] = SCROLL_BOTH_RESET
            print("  -> Both with reset (menu button)")
            break
        else:
            print("  ! Please enter 1, 2, 3, or 4")
    
    # --- Check if user wants to edit deck settings ---
    if has_saved:
        print(f"\nFound saved deck configuration for {len(saved_decks)} deck(s).")
        choice = input("Edit deck settings? (ENTER to keep, 'e' to edit): ").strip().lower()
        if choice != 'e':
            # Save brightness/scroll changes but keep deck config
            save_config(new_config)
            print("\nConfiguration saved!")
            print("=" * 60 + "\n")
            return new_config
    
    # --- Deck Assignment ---
    print("\n--- Deck Assignment ---")
    print(f"Found {len(discovered_decks)} StreamDeck(s).")
    print("For each deck, enter a display order (1-10), 0 to skip, or ENTER to keep.\n")
    
    used_orders = set()
    # Pre-populate used_orders from saved config
    for serial, deck_cfg in new_config["decks"].items():
        used_orders.add(deck_cfg["display_order"] + 1)  # Convert back to 1-indexed
    
    for deck in discovered_decks:
        serial = deck.get_serial_number()
        
        # Light up this deck
        light_deck(deck, on=True)
        
        # Get current settings for this deck
        current_cfg = saved_decks.get(serial, {})
        current_order = current_cfg.get("display_order", -1) + 1  # 1-indexed, 0 if not set
        current_offset = current_cfg.get("h_offset", 0)
        
        order_hint = f"current={current_order}" if current_order > 0 else "not configured"
        
        while True:
            try:
                order_input = input(f"Display order for currently lit deck ({serial}) [0=skip, 1-10, {order_hint}]: ").strip()
                
                # ENTER keeps current
                if order_input == "":
                    if current_order > 0:
                        print(f"  -> Keeping deck #{current_order}")
                        light_deck(deck, on=False)
                        break
                    else:
                        print("  ! No current setting. Please enter 0-10.")
                        continue
                
                order = int(order_input)
                
                if order == 0:
                    # Remove from config if skipping
                    if serial in new_config["decks"]:
                        old_order = new_config["decks"][serial]["display_order"] + 1
                        used_orders.discard(old_order)
                        del new_config["decks"][serial]
                    print(f"  -> Skipping this deck\n")
                    light_deck(deck, on=False)
                    break
                elif 1 <= order <= 10:
                    # Check if order is used by another deck
                    if order in used_orders and order != current_order:
                        print(f"  ! Order {order} already used. Choose a different number.")
                        continue
                    
                    # Remove old order from used set
                    if current_order > 0:
                        used_orders.discard(current_order)
                    used_orders.add(order)
                    
                    # Ask for horizontal offset
                    offset_hint = f"current={current_offset}" if serial in saved_decks else f"auto={(order - 1) * 8}"
                    while True:
                        try:
                            offset_input = input(f"  Horizontal offset for deck #{order} [0-32, {offset_hint}]: ").strip()
                            if offset_input == "":
                                if serial in saved_decks:
                                    h_offset = current_offset
                                    print(f"    Keeping offset: {h_offset}")
                                else:
                                    h_offset = (order - 1) * 8
                                    print(f"    Using auto-offset: {h_offset}")
                            else:
                                h_offset = int(offset_input)
                            
                            if 0 <= h_offset <= 32:
                                new_config["decks"][serial] = {
                                    "display_order": order - 1,
                                    "h_offset": h_offset
                                }
                                print(f"  -> Deck #{order} at h_offset {h_offset}\n")
                                light_deck(deck, on=False)
                                break
                            else:
                                print("    ! Offset must be 0-32")
                        except ValueError:
                            print("    ! Please enter a valid number")
                    break
                else:
                    print("  ! Please enter 0-10")
            except ValueError:
                print("  ! Please enter a valid number")
        
        # Turn off deck light before moving to next
        light_deck(deck, on=False)
    
    if not new_config["decks"]:
        print("\n! No decks configured. Exiting.")
        sys.exit(1)
    
    # Save configuration
    save_config(new_config)
    print("\nConfiguration saved!")
    print("=" * 60 + "\n")
    
    return new_config


# =============================================================================
# COLOR UTILITIES
# =============================================================================

def ableton_color_to_rgb(color_value):
    r = (color_value >> 16) & 0xFF
    g = (color_value >> 8) & 0xFF
    b = color_value & 0xFF
    return (r, g, b)


def calculate_luminance(color):
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def adjust_color(color, saturation, brightness):
    r, g, b = color
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    s = max(0, min(1, s * saturation))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return int(r * 255 * brightness), int(g * 255 * brightness), int(b * 255 * brightness)


# =============================================================================
# TEXT RENDERING
# =============================================================================

def wrap_text(draw, text, font, max_width):
    def force_hyphenation(segment):
        parts = []
        current_line = ""
        for c in segment:
            test_line = current_line + c
            if draw.textbbox((0, 0), test_line, font=font)[2] > max_width:
                if not current_line:
                    parts.append(c + "-")
                    current_line = ""
                else:
                    if draw.textbbox((0, 0), current_line + "-", font=font)[2] <= max_width:
                        parts.append(current_line + "-")
                    else:
                        parts.append(current_line)
                    current_line = c
            else:
                current_line = test_line
        if current_line:
            parts.append(current_line)
        return parts

    words = text.split(' ')
    lines = []
    current_line = ""

    for word in words:
        test_line = (current_line + " " + word).strip() if current_line else word
        if draw.textbbox((0, 0), test_line, font=font)[2] <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = ""
            if draw.textbbox((0, 0), word, font=font)[2] > max_width:
                hyphenated = force_hyphenation(word)
                for part in hyphenated:
                    if draw.textbbox((0, 0), part, font=font)[2] <= max_width:
                        lines.append(part)
                    else:
                        lines.append(part[:1] + "-")
                        lines.append(part[1:])
            else:
                current_line = word

    if current_line:
        lines.append(current_line)

    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-1] + "…"

    return lines


# =============================================================================
# KEY IMAGE RENDERING
# =============================================================================

def draw_rounded_rectangle_antialiased(base_image, x, y, width, height, radius, fill):
    scale = 4
    aa_image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    aa_draw = ImageDraw.Draw(aa_image)
    aa_draw.rounded_rectangle(
        [(0, 0), (width * scale, height * scale)],
        radius=radius * scale,
        fill=fill
    )
    aa_image = aa_image.resize((width, height), Image.LANCZOS)
    base_image.paste(aa_image, (x, y), aa_image.split()[3])


def render_key_image(deck, label_text, color, progress=None, background_color=None, stopped_keys=None, key=None, flash=False, scroll_mode=None, menu_active=False):
    image = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(image)
    w, h = image.width, image.height

    # Flash mode: entire key is 85% white, skip all other rendering
    if flash:
        draw.rectangle([(0, 0), (w, h)], fill=FLASH_COLOR)
        return PILHelper.to_native_key_format(deck, image.convert("RGB"))

    if progress is not None:
        if progress == -2:
            if stopped_keys and key in stopped_keys:
                background_color = (255, 0, 0)
            else:
                background_color = (0, 0, 0)
        if progress >= 0 and progress <= 1:
            background_color = adjust_color(color, 1, 0.2)
    
    draw.rectangle([(0, 0), (w, h)], fill=background_color or "black")

    if progress == -1:
        offset = w // 2
        draw.pieslice((-offset, -offset, w + offset, h + offset), -90, 270, fill=(230, 230, 230))
    elif progress is not None and progress >= 0 and progress <= 1:
        start_angle = -90
        end_angle = start_angle + progress * 360
        offset = w // 2
        adjusted_color = adjust_color(color, 1, 0.72)
        draw.pieslice((-offset, -offset, w + offset, h + offset), start_angle, end_angle, fill=adjusted_color)

    side = int(min(w, h) * 0.75)
    left = (w - side) // 2
    top = (h - side) // 2
    if progress is not None and progress >= 0 and progress <= 1:
        draw_rounded_rectangle_antialiased(
            image, left-4, top-4, side+8, side+8, 14, adjust_color(color, 0.7, 2.5)
        )
    draw_rounded_rectangle_antialiased(
        image, left, top, side, side, 10, color
    )

    font = ImageFont.truetype("Archivo_SemiCondensed-Regular.ttf", 18)
    text_color = "white"
    if calculate_luminance(color) > 128:
        text_color = "black"

    max_text_width = int(side * 0.92)
    lines = wrap_text(draw, label_text, font, max_text_width)
    text_height = draw.textbbox((0, 0), 'A', font=font)[3] - draw.textbbox((0, 0), 'A', font=font)[1]
    total_text_height = text_height * len(lines) * 1.5
    text_y = top + (side - total_text_height) // 2

    for line in lines:
        text_width = draw.textbbox((0, 0), line, font=font)[2] - draw.textbbox((0, 0), line, font=font)[0]
        text_x = left + (side - text_width) // 2
        draw.text((text_x, text_y), line, font=font, fill=text_color)
        text_y += text_height * 1.5

    # Scroll overlays based on scroll_mode
    if scroll_mode == SCROLL_VERTICAL:
        # Vertical mode: up arrow on key 30, down arrow on key 31
        if key == 30:
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 200))
            draw_overlay = ImageDraw.Draw(overlay)
            draw_overlay.polygon([(w // 2 - 10, h // 2 + 10), (w // 2, h // 2 - 10), (w // 2 + 10, h // 2 + 10)], fill="white")
            image = Image.alpha_composite(image.convert("RGBA"), overlay)
        elif key == 31:
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 200))
            draw_overlay = ImageDraw.Draw(overlay)
            draw_overlay.polygon([(w // 2 - 10, h // 2 - 10), (w // 2, h // 2 + 10), (w // 2 + 10, h // 2 - 10)], fill="white")
            image = Image.alpha_composite(image.convert("RGBA"), overlay)
    
    elif scroll_mode == SCROLL_BOTH:
        # Both mode: burger menu on key 31, brightness on 23, arrows appear when menu_active
        if key == KEY_BURGER:
            # Draw burger/menu icon (☰)
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 200))
            draw_overlay = ImageDraw.Draw(overlay)
            line_width = 3
            line_length = 20
            cx, cy = w // 2, h // 2
            for offset in [-8, 0, 8]:
                draw_overlay.rectangle(
                    [(cx - line_length // 2, cy + offset - line_width // 2),
                     (cx + line_length // 2, cy + offset + line_width // 2)],
                    fill="white"
                )
            image = Image.alpha_composite(image.convert("RGBA"), overlay)
        elif menu_active:
            # Draw arrows and brightness only when menu is active
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 200))
            draw_overlay = ImageDraw.Draw(overlay)
            cx, cy = w // 2, h // 2
            
            if key == KEY_ARROW_UP:
                draw_overlay.polygon([(cx - 10, cy + 10), (cx, cy - 10), (cx + 10, cy + 10)], fill="white")
            elif key == KEY_ARROW_DOWN:
                draw_overlay.polygon([(cx - 10, cy - 10), (cx, cy + 10), (cx + 10, cy - 10)], fill="white")
            elif key == KEY_ARROW_LEFT:
                draw_overlay.polygon([(cx + 10, cy - 10), (cx - 10, cy), (cx + 10, cy + 10)], fill="white")
            elif key == KEY_ARROW_RIGHT:
                draw_overlay.polygon([(cx - 10, cy - 10), (cx + 10, cy), (cx - 10, cy + 10)], fill="white")
            elif key == KEY_BRIGHTNESS:
                # Draw sun icon with brightness level
                cx, cy = w // 2, h // 2 - 12
                r = 8
                draw_overlay.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill="white")
                ray_len = 6
                ray_dist = 12
                for angle in range(0, 360, 45):
                    rad = math.radians(angle)
                    x1 = cx + int(ray_dist * math.cos(rad))
                    y1 = cy + int(ray_dist * math.sin(rad))
                    x2 = cx + int((ray_dist + ray_len) * math.cos(rad))
                    y2 = cy + int((ray_dist + ray_len) * math.sin(rad))
                    draw_overlay.line([(x1, y1), (x2, y2)], fill="white", width=2)
                try:
                    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
                except:
                    font = ImageFont.load_default()
                brightness = current_config.get("current_brightness", current_config.get("brightness", 3))
                draw_overlay.text((cx, cy + 36), str(brightness), font=font, fill="white", anchor="mm")
            
            if key in {KEY_ARROW_UP, KEY_ARROW_DOWN, KEY_ARROW_LEFT, KEY_ARROW_RIGHT, KEY_BRIGHTNESS}:
                image = Image.alpha_composite(image.convert("RGBA"), overlay)

    elif scroll_mode == SCROLL_BOTH_RESET:
        # Both-reset mode: like both, but left arrow shows reset icon instead
        if key == KEY_BURGER:
            # Draw burger/menu icon (☰)
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 200))
            draw_overlay = ImageDraw.Draw(overlay)
            line_width = 3
            line_length = 20
            cx, cy = w // 2, h // 2
            for offset in [-8, 0, 8]:
                draw_overlay.rectangle(
                    [(cx - line_length // 2, cy + offset - line_width // 2),
                     (cx + line_length // 2, cy + offset + line_width // 2)],
                    fill="white"
                )
            image = Image.alpha_composite(image.convert("RGBA"), overlay)
        elif menu_active:
            # Draw arrows, reset, and brightness only when menu is active
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 200))
            draw_overlay = ImageDraw.Draw(overlay)
            cx, cy = w // 2, h // 2
            
            if key == KEY_ARROW_UP:
                draw_overlay.polygon([(cx - 10, cy + 10), (cx, cy - 10), (cx + 10, cy + 10)], fill="white")
            elif key == KEY_ARROW_DOWN:
                draw_overlay.polygon([(cx - 10, cy - 10), (cx, cy + 10), (cx + 10, cy - 10)], fill="white")
            elif key == KEY_ARROW_LEFT:
                # Rewind icon: two left-pointing arrows (⏪)
                draw_overlay.polygon([(cx + 2, cy - 10), (cx - 8, cy), (cx + 2, cy + 10)], fill="white")
                draw_overlay.polygon([(cx + 12, cy - 10), (cx + 2, cy), (cx + 12, cy + 10)], fill="white")
            elif key == KEY_ARROW_RIGHT:
                draw_overlay.polygon([(cx - 10, cy - 10), (cx + 10, cy), (cx - 10, cy + 10)], fill="white")
            elif key == KEY_BRIGHTNESS:
                # Draw sun icon with brightness level
                cx, cy = w // 2, h // 2 - 12
                r = 8
                draw_overlay.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill="white")
                ray_len = 6
                ray_dist = 12
                for angle in range(0, 360, 45):
                    rad = math.radians(angle)
                    x1 = cx + int(ray_dist * math.cos(rad))
                    y1 = cy + int(ray_dist * math.sin(rad))
                    x2 = cx + int((ray_dist + ray_len) * math.cos(rad))
                    y2 = cy + int((ray_dist + ray_len) * math.sin(rad))
                    draw_overlay.line([(x1, y1), (x2, y2)], fill="white", width=2)
                try:
                    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
                except:
                    font = ImageFont.load_default()
                brightness = current_config.get("current_brightness", current_config.get("brightness", 3))
                draw_overlay.text((cx, cy + 36), str(brightness), font=font, fill="white", anchor="mm")
            
            if key in {KEY_ARROW_UP, KEY_ARROW_DOWN, KEY_ARROW_LEFT, KEY_ARROW_RIGHT, KEY_BRIGHTNESS}:
                image = Image.alpha_composite(image.convert("RGBA"), overlay)

    return PILHelper.to_native_key_format(deck, image.convert("RGB"))


def update_key_image(deck, key, label_text, color, progress=None, background_color=None, stopped_keys=None, flash=False, scroll_mode=None, menu_active=False):
    if deck is None or key < 0 or key >= deck.key_count():
        return False
    try:
        deck.get_serial_number()
    except:
        # Deck disconnected
        return False
    try:
        image = render_key_image(deck, label_text, color, progress, background_color, stopped_keys, key, flash=flash, scroll_mode=scroll_mode, menu_active=menu_active)
        with deck:
            deck.set_key_image(key, image)
    except:
        return False
    return True


# =============================================================================
# DECK MANAGER
# Manages multiple StreamDecks and their state
# =============================================================================

class DeckManager:
    def __init__(self):
        self.decks = []  # All discovered decks
        self.deck_states = {}  # display_order -> state dict
        self.serial_to_deck = {}  # serial -> deck
        self.serial_to_order = {}  # serial -> display_order
        self.order_to_deck = {}  # display_order -> deck
        self._config = {}  # Store config
        
    def discover_decks(self):
        streamdecks = DeviceManager().enumerate()
        for d in streamdecks:
            try:
                d.open()
                d.reset()
                self.decks.append(d)
                self.serial_to_deck[d.get_serial_number()] = d
            except:
                continue
        return self.decks
    
    def configure_from_config(self, config):
        """Set up deck mappings based on configuration."""
        self._config = config  # Store for reconnection
        deck_configs = config.get("decks", {})
        
        for serial, deck_cfg in deck_configs.items():
            if serial in self.serial_to_deck:
                display_order = deck_cfg["display_order"]
                self.serial_to_order[serial] = display_order
                self.order_to_deck[display_order] = self.serial_to_deck[serial]
                self.init_cs_state(display_order)
    
    def get_deck_for_cs(self, display_order):
        return self.order_to_deck.get(display_order)
    
    def get_config_for_order(self, display_order, config):
        """Get h_offset for a display_order from config."""
        for serial, deck_cfg in config.get("decks", {}).items():
            if deck_cfg["display_order"] == display_order:
                return deck_cfg
        return None
    
    def init_cs_state(self, display_order):
        if display_order not in self.deck_states:
            self.deck_states[display_order] = {
                'key_states': {},
                'stopped_keys': set(),
                'last_ablonline': 0,
                'waiting_cleared': False,
                'structural_mismatch': False,
                'pressed_keys': set(),
                'flash_until': {},  # key -> time when flash should end
                'menu_active': False,  # For SCROLL_BOTH mode
                'ableton_offline': True  # Start as offline until we hear from Ableton
            }
    
    def get_cs_state(self, display_order):
        self.init_cs_state(display_order)
        return self.deck_states[display_order]
    
    def get_active_display_orders(self):
        return list(self.order_to_deck.keys())
    
    def close_deck(self, display_order):
        """Close a deck."""
        if display_order in self.order_to_deck:
            try:
                self.order_to_deck[display_order].close()
            except:
                pass


# =============================================================================
# OSC MESSAGE HANDLERS
# =============================================================================

deck_manager = DeckManager()
osc_clients = {}  # display_order -> client
current_config = {}  # Will be set after interactive setup


def get_or_create_client(display_order):
    if display_order not in osc_clients:
        port = 9001 + (display_order * 10)
        osc_clients[display_order] = udp_client.SimpleUDPClient("127.0.0.1", port)
    return osc_clients[display_order]


# Helper to send to all clients (normal + debug)
def send_osc_message(display_order, address, *args):
    client = get_or_create_client(display_order)
    client.send_message(address, args)
    if debug_mode:
        # Send to debug port (normal port + 1000)
        debug_port = 9001 + (display_order * 10) + 1000
        if f"debug_{display_order}" not in osc_clients:
            osc_clients[f"debug_{display_order}"] = udp_client.SimpleUDPClient("127.0.0.1", debug_port)
        osc_clients[f"debug_{display_order}"].send_message(address, args)


def osc_clip_info_handler(unused_addr, *args):
    if debug_mode:
        print(f"[DEBUG] OSC recv: {unused_addr} args_count={len(args)}")
    if len(args) < 2:
        return
    display_order = int(args[0])
    clip_data = args[1:]
    
    deck = deck_manager.get_deck_for_cs(display_order)
    if deck is None:
        return
    
    state = deck_manager.get_cs_state(display_order)
    state['last_ablonline'] = time.time()  # Update on any message from Ableton
    
    # Clear offline state if we were offline
    if state.get('ableton_offline', False):
        state['ableton_offline'] = False
        state['waiting_cleared'] = True  # Will be redrawn with clip info
    
    pressed_keys = state.get('pressed_keys', set())
    flash_until = state.get('flash_until', {})
    current_time = time.time()
    scroll_mode = current_config.get("scroll_mode", SCROLL_VERTICAL)
    menu_active = state.get('menu_active', False)
    
    for i, info in enumerate(clip_data):
        if i < 32:
            # Skip updating keys that are flashing (either pressed or within flash duration)
            if i in pressed_keys or (i in flash_until and current_time < flash_until[i]):
                # Still update the state cache so release shows correct info
                parts = info.split('|', 2)
                if len(parts) == 3:
                    try:
                        state['key_states'][i] = (parts[0], int(parts[1]), float(parts[2]))
                    except ValueError:
                        pass
                continue
                
            parts = info.split('|', 2)
            if len(parts) == 3:
                clip_name, clip_color, clip_progress = parts
                try:
                    clip_color_val = int(clip_color)
                    clip_progress_value = float(clip_progress)
                    new_state = (clip_name, clip_color_val, clip_progress_value)
                    
                    if state['key_states'].get(i) != new_state:
                        state['key_states'][i] = new_state
                        rgba_color = ableton_color_to_rgb(clip_color_val)
                        if clip_progress_value >= 0:
                            fraction = clip_progress_value / 16.0
                            if fraction == 0.0:
                                fraction = 1.0
                            update_key_image(deck, i, clip_name, rgba_color, progress=fraction, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=menu_active)
                        elif clip_progress_value == -1:
                            update_key_image(deck, i, clip_name, rgba_color, progress=-1, scroll_mode=scroll_mode, menu_active=menu_active)
                        elif clip_progress_value == -3:
                            update_key_image(deck, i, clip_name, rgba_color, progress=-2, background_color=(255, 0, 0), scroll_mode=scroll_mode, menu_active=menu_active)
                        else:
                            update_key_image(deck, i, clip_name, rgba_color, progress=-2, scroll_mode=scroll_mode, menu_active=menu_active)
                except ValueError:
                    pass


def osc_ablonline_handler(unused_addr, *args):
    if debug_mode:
        print(f"[DEBUG] OSC recv: {unused_addr} {args}")
    display_order = int(args[0]) if args else 0
    state = deck_manager.get_cs_state(display_order)
    deck = deck_manager.get_deck_for_cs(display_order)
    
    state['last_ablonline'] = time.time()
    
    # Clear offline state
    if state.get('ableton_offline', False):
        state['ableton_offline'] = False
    
    if not state['waiting_cleared'] and deck:
        update_key_image(deck, 0, "", (0, 0, 0), stopped_keys=state['stopped_keys'])
        state['waiting_cleared'] = True


def osc_structural_mismatch_handler(unused_addr, *args):
    if debug_mode:
        print(f"[DEBUG] OSC recv: {unused_addr} {args}")
    if len(args) < 2:
        return
    display_order = int(args[0])
    show = bool(args[1])
    
    state = deck_manager.get_cs_state(display_order)
    deck = deck_manager.get_deck_for_cs(display_order)
    
    state['structural_mismatch'] = show
    if deck:
        if show:
            update_key_image(deck, 0, "Wrong Channel Number", (255, 125, 0))
            state['waiting_cleared'] = True
        else:
            update_key_image(deck, 0, "", (0, 0, 0))
            state['waiting_cleared'] = False


def osc_document_closing_handler(unused_addr, *args):
    if debug_mode:
        print(f"[DEBUG] OSC recv: {unused_addr} {args}")
    display_order = int(args[0]) if args else 0
    state = deck_manager.get_cs_state(display_order)
    deck = deck_manager.get_deck_for_cs(display_order)
    
    if deck:
        for key in range(deck.key_count()):
            update_key_image(deck, key, "", (0, 0, 0))
    state['key_states'].clear()
    state['last_ablonline'] = 0
    state['waiting_cleared'] = False


def osc_track_stopped_handler(unused_addr, *args):
    if debug_mode:
        print(f"[DEBUG] OSC recv: {unused_addr} {args}")
    if len(args) < 3:
        return
    display_order = int(args[0])
    track_index = int(args[1])
    was_playing = bool(args[2])
    
    state = deck_manager.get_cs_state(display_order)
    deck = deck_manager.get_deck_for_cs(display_order)
    
    if deck is None:
        return
    
    display_time = 4.0 if was_playing else 0.5
    for scene_index in range(4):
        key = scene_index * 8 + track_index
        if key in state['key_states']:
            label_text, color_val, progress = state['key_states'][key]
            color = ableton_color_to_rgb(color_val)
        else:
            label_text, color, progress = "", (0, 0, 0), None
        state['stopped_keys'].add(key)
        update_key_image(deck, key, label_text, color, progress, background_color=(255, 0, 0), stopped_keys=state['stopped_keys'])
        threading.Timer(
            display_time,
            lambda k=key, lt=label_text, c=color, p=progress, d=deck, s=state: (
                s['stopped_keys'].discard(k),
                update_key_image(d, k, lt, c, p, stopped_keys=s['stopped_keys'])
            )
        ).start()


def osc_config_request_handler(unused_addr, *args):
    """Handle config request from Control Surface."""
    if debug_mode:
        print(f"[DEBUG] OSC recv: {unused_addr} {args}")
    if len(args) < 1:
        return
    display_order = int(args[0])
    
    # Look up config for this display_order
    deck_cfg = deck_manager.get_config_for_order(display_order, current_config)
    if deck_cfg:
        client = get_or_create_client(display_order)
        # Send config: /config <display_order> <h_offset>
        send_osc_message(display_order, "/config", display_order, deck_cfg["h_offset"])


# =============================================================================
# KEY INPUT HANDLING
# =============================================================================

def on_key_change(deck, key, display_order, client):
    scroll_mode = current_config.get("scroll_mode", SCROLL_VERTICAL)
    
    if scroll_mode == SCROLL_VERTICAL:
        # Vertical mode: keys 30/31 are up/down
        if key == 30:
            send_osc_message(display_order, "/scroll", "up")
            return
        elif key == 31:
            send_osc_message(display_order, "/scroll", "down")
            return
    
    elif scroll_mode == SCROLL_BOTH:
        state = deck_manager.get_cs_state(display_order)
        # In both mode, arrow keys only work when menu is active
        if state.get('menu_active', False):
            if key == KEY_ARROW_UP:
                send_osc_message(display_order, "/scroll", "up")
                return
            elif key == KEY_ARROW_DOWN:
                send_osc_message(display_order, "/scroll", "down")
                return
            elif key == KEY_ARROW_LEFT:
                send_osc_message(display_order, "/scroll", "left")
                return
            elif key == KEY_ARROW_RIGHT:
                send_osc_message(display_order, "/scroll", "right")
                return
        # Burger and brightness keys don't trigger clip action
        if key == KEY_BURGER or key == KEY_BRIGHTNESS:
            return
    
    elif scroll_mode == SCROLL_BOTH_RESET:
        state = deck_manager.get_cs_state(display_order)
        # In both-reset mode, arrow keys only work when menu is active
        if state.get('menu_active', False):
            if key == KEY_ARROW_UP:
                send_osc_message(display_order, "/scroll", "up")
                return
            elif key == KEY_ARROW_DOWN:
                send_osc_message(display_order, "/scroll", "down")
                return
            elif key == KEY_ARROW_LEFT:
                # Reset to original position from config
                send_osc_message(display_order, "/scroll", "reset")
                return
            elif key == KEY_ARROW_RIGHT:
                send_osc_message(display_order, "/scroll", "right")
                return
        # Burger and brightness keys don't trigger clip action
        if key == KEY_BURGER or key == KEY_BRIGHTNESS:
            return
    
    # Normal clip trigger
    track_offset = key % 8
    scene_offset = key // 8
    send_osc_message(display_order, "/trigger_clip", track_offset, scene_offset)


def restore_key_image(deck, key, state):
    """Restore a key to its current state (called after flash duration)."""
    scroll_mode = current_config.get("scroll_mode", SCROLL_VERTICAL)
    menu_active = state.get('menu_active', False)
    
    if key in state['key_states']:
        label, color_val, progress = state['key_states'][key]
        color = ableton_color_to_rgb(color_val) if color_val else (0, 0, 0)
        if progress is not None and progress >= 0:
            fraction = progress / 16.0 if progress > 0 else 1.0
            update_key_image(deck, key, label, color, progress=fraction, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=menu_active)
        elif progress == -1:
            update_key_image(deck, key, label, color, progress=-1, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=menu_active)
        elif progress == -3:
            update_key_image(deck, key, label, color, progress=-2, background_color=(255, 0, 0), stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=menu_active)
        else:
            update_key_image(deck, key, label, color, progress=-2, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=menu_active)
    else:
        update_key_image(deck, key, "", (0, 0, 0), scroll_mode=scroll_mode, menu_active=menu_active)


def show_menu_arrows(deck, display_order, show):
    """Show or hide the arrow keys and brightness button around the burger menu."""
    state = deck_manager.get_cs_state(display_order)
    scroll_mode = current_config.get("scroll_mode", SCROLL_VERTICAL)
    
    for key in [KEY_ARROW_UP, KEY_ARROW_DOWN, KEY_ARROW_LEFT, KEY_ARROW_RIGHT, KEY_BRIGHTNESS]:
        if key in state['key_states']:
            label, color_val, progress = state['key_states'][key]
            color = ableton_color_to_rgb(color_val) if color_val else (0, 0, 0)
            if progress is not None and progress >= 0:
                fraction = progress / 16.0 if progress > 0 else 1.0
                update_key_image(deck, key, label, color, progress=fraction, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=show)
            elif progress == -1:
                update_key_image(deck, key, label, color, progress=-1, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=show)
            elif progress == -3:
                update_key_image(deck, key, label, color, progress=-2, background_color=(255, 0, 0), stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=show)
            else:
                update_key_image(deck, key, label, color, progress=-2, stopped_keys=state['stopped_keys'], scroll_mode=scroll_mode, menu_active=show)
        else:
            update_key_image(deck, key, "", (0, 0, 0), scroll_mode=scroll_mode, menu_active=show)


def start_key_polling(deck, display_order, client):
    previous_key_states = [False] * deck.key_count()
    brightness_press_start = None  # Track when brightness was pressed for long-press detection
    LONG_PRESS_DURATION = 0.5  # seconds for long press
    
    def poll_keys():
        nonlocal previous_key_states, brightness_press_start
        while True:
            try:
                current_key_states = deck.key_states()
                state = deck_manager.get_cs_state(display_order)
                pressed_keys = state['pressed_keys']
                flash_until = state['flash_until']
                current_time = time.time()
                scroll_mode = current_config.get("scroll_mode", SCROLL_VERTICAL)
                
                # Handle burger menu in SCROLL_BOTH or SCROLL_BOTH_RESET mode
                if scroll_mode in (SCROLL_BOTH, SCROLL_BOTH_RESET):
                    burger_pressed = current_key_states[KEY_BURGER]
                    burger_was_pressed = previous_key_states[KEY_BURGER]
                    
                    if burger_pressed and not burger_was_pressed:
                        # Burger just pressed - show arrows
                        state['menu_active'] = True
                        show_menu_arrows(deck, display_order, True)
                    elif not burger_pressed and burger_was_pressed:
                        # Burger just released - hide arrows
                        state['menu_active'] = False
                        show_menu_arrows(deck, display_order, False)
                    
                    # Handle brightness button only when menu is active
                    if state.get('menu_active', False):
                        brightness_pressed = current_key_states[KEY_BRIGHTNESS]
                        brightness_was_pressed = previous_key_states[KEY_BRIGHTNESS]
                        
                        if brightness_pressed and not brightness_was_pressed:
                            # Brightness just pressed - start timer
                            brightness_press_start = current_time
                        elif not brightness_pressed and brightness_was_pressed:
                            # Brightness just released - check if short or long press
                            if brightness_press_start is not None:
                                press_duration = current_time - brightness_press_start
                                saved_brightness = current_config.get("brightness", 3)
                                
                                if press_duration >= LONG_PRESS_DURATION:
                                    # Long press - reset to saved brightness
                                    current_config["current_brightness"] = saved_brightness
                                else:
                                    # Short press - cycle brightness (0-10, wrap at 11)
                                    current_brightness = current_config.get("current_brightness", saved_brightness)
                                    new_brightness = (current_brightness + 1) % 11
                                    current_config["current_brightness"] = new_brightness
                                
                                # Apply brightness to all active decks
                                actual_brightness = current_config.get("current_brightness", saved_brightness)
                                for order in deck_manager.get_active_display_orders():
                                    d = deck_manager.get_deck_for_cs(order)
                                    if d:
                                        set_brightness(d, actual_brightness)
                                
                                # Update brightness button display
                                update_key_image(deck, KEY_BRIGHTNESS, "", (0, 0, 0), scroll_mode=scroll_mode, menu_active=state.get('menu_active', False))
                                
                                brightness_press_start = None
                    else:
                        # Reset brightness tracking when menu not active
                        brightness_press_start = None
                
                for key in range(deck.key_count()):
                    # Key just pressed
                    if current_key_states[key] and not previous_key_states[key]:
                        pressed_keys.add(key)
                        
                        # Flash all keys including scroll keys
                        flash_until[key] = current_time + FLASH_DURATION
                        update_key_image(deck, key, "", (0, 0, 0), flash=True)
                        
                        on_key_change(deck, key, display_order, client)
                    
                    # Key just released
                    elif not current_key_states[key] and previous_key_states[key]:
                        pressed_keys.discard(key)
                        
                        # Check if flash duration has passed
                        if key in flash_until:
                            remaining = flash_until[key] - current_time
                            if remaining > 0:
                                # Schedule restore after remaining flash time
                                threading.Timer(
                                    remaining,
                                    lambda k=key, d=deck, s=state: (
                                        s['flash_until'].pop(k, None),
                                        restore_key_image(d, k, s)
                                    )
                                ).start()
                            else:
                                # Flash time already passed, restore immediately
                                flash_until.pop(key, None)
                                restore_key_image(deck, key, state)
                
                previous_key_states = current_key_states.copy()
            except:
                pass
            time.sleep(0.02)
    
    polling_thread = threading.Thread(target=poll_keys, daemon=True)
    polling_thread.start()
    return polling_thread


# =============================================================================
# STREAMDECK UTILITIES
# =============================================================================

def set_brightness(deck, brightness):
    if 0 <= brightness <= 10:
        # Map 0-10 to 0-100%
        deck.set_brightness(brightness * 10)


# =============================================================================
# PYONLINE SENDER & CONFIG BROADCASTER
# =============================================================================

def start_pyonline_sender():
    """Periodically send config to all Control Surfaces.
    This ensures CS gets config even if it starts after ClipDeck."""
    def send_config_loop():
        while True:
            time.sleep(3)
            for display_order in deck_manager.get_active_display_orders():
                deck_cfg = deck_manager.get_config_for_order(display_order, current_config)
                if deck_cfg:
                    client = get_or_create_client(display_order)
                    # Send config: /config <display_order> <h_offset> <debug_mode>
                    send_osc_message(display_order, "/config", display_order, deck_cfg["h_offset"], int(debug_mode))
    threading.Thread(target=send_config_loop, daemon=True).start()


def start_offline_monitor():
    """Monitor for Ableton going offline (no messages for 2+ seconds)."""
    OFFLINE_TIMEOUT = 2.0
    
    def monitor_loop():
        while True:
            time.sleep(0.5)  # Check every 0.5 seconds
            current_time = time.time()
            
            for display_order in deck_manager.get_active_display_orders():
                state = deck_manager.get_cs_state(display_order)
                deck = deck_manager.get_deck_for_cs(display_order)
                
                if deck is None:
                    continue
                
                last_msg = state.get('last_ablonline', 0)
                was_offline = state.get('ableton_offline', True)
                
                # Check if we've timed out
                if last_msg > 0 and (current_time - last_msg) > OFFLINE_TIMEOUT:
                    if not was_offline:
                        # Just went offline
                        state['ableton_offline'] = True
                        state['waiting_cleared'] = False
                        # Clear all keys and show offline message
                        for key in range(deck.key_count()):
                            update_key_image(deck, key, "", (0, 0, 0))
                        update_key_image(deck, 0, "Ableton Offline", (255, 80, 0))
                        print(f"[WARN] Ableton offline for deck {display_order}")
    
    threading.Thread(target=monitor_loop, daemon=True).start()


def send_initial_config():
    """Send configuration to all Control Surfaces."""
    for display_order in deck_manager.get_active_display_orders():
        deck_cfg = deck_manager.get_config_for_order(display_order, current_config)
        if deck_cfg:
            client = get_or_create_client(display_order)
            # Send config: /config <display_order> <h_offset> <debug_mode>
            send_osc_message(display_order, "/config", display_order, deck_cfg["h_offset"], int(debug_mode))


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # Check for 'settings' argument
    force_menu = "settings" in sys.argv
    
    # Discover all StreamDecks
    discovered = deck_manager.discover_decks()
    
    if len(discovered) == 0:
        print("No StreamDecks found. Exiting.")
        sys.exit(1)
    
    # Configuration (interactive if 'settings' or no valid saved config)
    current_config = interactive_setup(discovered, force_menu=force_menu)
    
    # Configure deck manager with the selected config
    deck_manager.configure_from_config(current_config)
    
    if not deck_manager.get_active_display_orders():
        print("No decks configured. Exiting.")
        sys.exit(1)
    
    # Brightness from config
    brightness = current_config.get("brightness", 3)
    
    # Initialize all active decks
    for display_order in deck_manager.get_active_display_orders():
        deck = deck_manager.get_deck_for_cs(display_order)
        if deck:
            set_brightness(deck, brightness)
            for key in range(deck.key_count()):
                update_key_image(deck, key, "", (0, 0, 0))
            update_key_image(deck, 0, "Waiting for Ableton", (255, 255, 255))
    
    # Start pyonline sender
    start_pyonline_sender()
    
    # Start offline monitor
    start_offline_monitor()
    
    # Set up OSC dispatcher
    disp = dispatcher.Dispatcher()
    disp.map("/clip_info", osc_clip_info_handler)
    disp.map("/ablonline", osc_ablonline_handler)
    disp.map("/structural_mismatch", osc_structural_mismatch_handler)
    disp.map("/document_closing", osc_document_closing_handler)
    disp.map("/track_stopped", osc_track_stopped_handler)
    disp.map("/config_request", osc_config_request_handler)
    
    # Listen on ports for each configured control surface
    servers = []
    for display_order in deck_manager.get_active_display_orders():
        port = 9000 + (display_order * 10)
        server = osc_server.ThreadingOSCUDPServer(("127.0.0.1", port), disp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        get_or_create_client(display_order)
    
    # Start key polling for each active deck
    for display_order in deck_manager.get_active_display_orders():
        deck = deck_manager.get_deck_for_cs(display_order)
        client = get_or_create_client(display_order)
        start_key_polling(deck, display_order, client)
    
    # Send initial config (CS may already be waiting)
    time.sleep(0.5)
    send_initial_config()
    
    if debug_mode:
        print("\n[DEBUG] Debug mode is ENABLED - logging all OSC messages")
    print("\nClipDeck running. Press 'c' to exit.\n")
    
    def cleanup_and_exit():
        """Clear all decks to black and close them."""
        print("\nShutting down...")
        for display_order in deck_manager.get_active_display_orders():
            deck = deck_manager.get_deck_for_cs(display_order)
            if deck:
                try:
                    # Clear all keys to black
                    for key in range(deck.key_count()):
                        image = PILHelper.create_image(deck)
                        draw = ImageDraw.Draw(image)
                        draw.rectangle([(0, 0), (image.width, image.height)], fill=(0, 0, 0))
                        native_image = PILHelper.to_native_key_format(deck, image.convert("RGB"))
                        with deck:
                            deck.set_key_image(key, native_image)
                    deck.close()
                except:
                    pass
        sys.exit(0)
    
    # Keep main thread alive, listen for 'c' to quit
    import select
    import sys
    import tty
    import termios
    
    # Save terminal settings
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        # Set terminal to raw mode to capture single keypress
        tty.setcbreak(sys.stdin.fileno())
        while True:
            # Check if there's input available
            if select.select([sys.stdin], [], [], 0.1)[0]:
                char = sys.stdin.read(1)
                if char.lower() == 'c':
                    cleanup_and_exit()
            time.sleep(0.1)
    except KeyboardInterrupt:
        cleanup_and_exit()
    finally:
        # Restore terminal settings
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
