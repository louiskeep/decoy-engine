# decoy_engine/utils/helpers.py
"""
General helper functions for the decoy_engine package.
"""

import hashlib
import hmac
from typing import Dict, Any, List, Optional, Callable
from faker import Faker


def deterministic_hash(value, seed=0):
    """
    Legacy SHA256(value + seed) hash. Kept for backwards compatibility when
    no master key is configured. Prefer ``hmac_hex`` (keyed) for any new
    code path so output is per-tenant and not derivable from the value alone.

    Args:
        value: The value to hash
        seed: A seed to ensure consistent hashing across runs

    Returns:
        A deterministic hash string
    """
    if value is None:
        return None

    # Convert to string and add seed
    value_str = f"{value}{seed}"

    # Create hash
    hash_obj = hashlib.sha256(value_str.encode())
    return hash_obj.hexdigest()


def hmac_hex(key: bytes, value) -> str:
    """HMAC-SHA256(key, value) as a 64-char hex string.

    The "Path B" deterministic primitive: same key + same input always
    yields the same output, with no per-tenant secret leakage (unlike
    SHA256(value + seed) where the seed is recoverable by brute force on
    a single known mapping).
    """
    if value is None:
        return None
    msg = str(value).encode("utf-8", errors="replace")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def hmac_seed(key: bytes, value) -> int:
    """Derive a 32-bit integer seed for Faker.seed_instance(...) from
    HMAC-SHA256(key, value). Same input + same key → same seed → same
    Faker output, with zero state stored anywhere.
    """
    if value is None:
        return 0
    msg = str(value).encode("utf-8", errors="replace")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big")


def get_faker_providers(faker_instance: Faker) -> Dict[str, Callable]:
    """
    Get a comprehensive dictionary of Faker providers
    
    Args:
        faker_instance: An initialized Faker instance
        
    Returns:
        Dictionary mapping provider names to faker functions
    """
    fake = faker_instance
    
    # Create a comprehensive dictionary of faker providers
    faker_providers = {
        # Person providers
        'first_name': lambda: fake.first_name(),
        'last_name': lambda: fake.last_name(),
        'name': lambda: fake.name(),
        'prefix': lambda: fake.prefix(),
        'suffix': lambda: fake.suffix(),
        
        # Contact providers
        'email': lambda: fake.email(),
        'phone_number': lambda: fake.phone_number(),
        'username': lambda: fake.user_name(),
        
        # Address providers
        'address': lambda: fake.address().replace('\n', ', '),
        'street_address': lambda: fake.street_address(),
        'city': lambda: fake.city(),
        'state': lambda: fake.state(),
        'state_abbr': lambda: fake.state_abbr(),
        'zipcode': lambda: fake.zipcode(),
        'country': lambda: fake.country(),
        
        # Company/job providers
        'company': lambda: fake.company(),
        'company_suffix': lambda: fake.company_suffix(),
        'job': lambda: fake.job(),
        
        # Finance providers
        'credit_card_number': lambda: fake.credit_card_number(),
        'credit_card_provider': lambda: fake.credit_card_provider(),
        'currency_code': lambda: fake.currency_code(),
        'ssn': lambda: fake.ssn(),
        
        # Date/time providers
        'date': lambda: fake.date_this_decade().strftime('%Y-%m-%d'),
        'date_of_birth': lambda: fake.date_of_birth().strftime('%Y-%m-%d'),
        'future_date': lambda: fake.future_date().strftime('%Y-%m-%d'),
        'past_date': lambda: fake.past_date().strftime('%Y-%m-%d'),
        'time': lambda: fake.time(),
        'day_of_week': lambda: fake.day_of_week(),
        'month': lambda: fake.month_name(),
        
        # Internet providers
        'domain': lambda: fake.domain_name(),
        'url': lambda: fake.url(),
        'ipv4': lambda: fake.ipv4(),
        'ipv6': lambda: fake.ipv6(),
        'user_agent': lambda: fake.user_agent(),
        
        # Text providers
        'word': lambda: fake.word(),
        'words': lambda n=3: ' '.join(fake.words(n)),
        'sentence': lambda: fake.sentence(),
        'paragraph': lambda: fake.paragraph(),
        'text': lambda: fake.text(max_nb_chars=100),
        
        # Misc providers
        'color': lambda: fake.color_name(),
        'color_hex': lambda: fake.hex_color(),
        'file_path': lambda: fake.file_path(),
        'file_name': lambda: fake.file_name(),
        'mime_type': lambda: fake.mime_type(),
        'uuid4': lambda: str(fake.uuid4()),
    }
    
    return faker_providers


def convert_quoting_mode(quoting_mode: str) -> int:
    """
    Convert a quoting mode string to the corresponding CSV module constant
    
    Args:
        quoting_mode: String representation of quoting mode
        
    Returns:
        Integer value matching csv module constants
    """
    quoting_map = {
        'minimal': 0,  # csv.QUOTE_MINIMAL
        'all': 1,      # csv.QUOTE_ALL
        'nonnumeric': 2,  # csv.QUOTE_NONNUMERIC
        'none': 3      # csv.QUOTE_NONE
    }
    return quoting_map.get(quoting_mode.lower(), 0)


def create_directory_for_file(file_path: str) -> None:
    """
    Create the directory for a file path if it doesn't exist
    
    Args:
        file_path: Path to a file
    """
    import os
    from pathlib import Path
    
    directory = os.path.dirname(file_path)
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)


def is_path_exists(path: str) -> bool:
    """
    Check if a path exists (file or directory)
    
    Args:
        path: Path to check
        
    Returns:
        True if path exists, False otherwise
    """
    import os
    return os.path.exists(path)


def get_filename_without_extension(file_path: str) -> str:
    """
    Get the filename without extension from a path
    
    Args:
        file_path: Path to a file
        
    Returns:
        Filename without extension
    """
    import os
    base_name = os.path.basename(file_path)
    return os.path.splitext(base_name)[0]


def convert_file_size(size_bytes: int) -> str:
    """
    Convert file size in bytes to a human-readable string
    
    Args:
        size_bytes: File size in bytes
        
    Returns:
        Human-readable file size string
    """
    # Define unit prefixes
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    
    # Special case for size=0
    if size_bytes == 0:
        return '0 B'
    
    # Determine the appropriate unit
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1
    
    # Format with appropriate precision
    if i == 0:  # Bytes
        return f"{size_bytes:.0f} {units[i]}"
    else:
        return f"{size_bytes:.2f} {units[i]}"


def get_file_size(file_path: str) -> Optional[int]:
    """
    Get the size of a file in bytes
    
    Args:
        file_path: Path to the file
        
    Returns:
        File size in bytes or None if file doesn't exist
    """
    import os
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return os.path.getsize(file_path)
    return None


def format_elapsed_time(seconds: float) -> str:
    """
    Format elapsed time in seconds to a human-readable string
    
    Args:
        seconds: Time in seconds
        
    Returns:
        Formatted time string
    """
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f} minutes"
    else:
        hours = seconds / 3600
        return f"{hours:.1f} hours"