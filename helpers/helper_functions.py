"""
This module provides helper functions.
"""

import os
import re
from typing import Dict, List, Union
from urllib.parse import unquote, urlparse

import pyodbc


def _is_url(string: str) -> bool:
    """
    Check if a given string is a valid URL.

    Args:
        string (str): The string to be checked.

    Returns:
        bool: True if the string is a valid URL, False otherwise.
    """
    url_pattern = re.compile(
        r"^(https?://)?"
        r"([a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,6})"
        r"(:[0-9]{1,5})?"
        r"(/.*)?$",
        re.IGNORECASE,
    )
    return re.match(url_pattern, string) is not None


def find_urls(data: Union[Dict[str, Union[str, dict, list]], list]) -> List[str]:
    """
    Recursively find all URLs in a nested dictionary or list.

    Args:
        data (Union[Dict[str, Union[str, dict, list]], list]): The data to search for URLs.

    Returns:
        List[str]: A list of found URLs.
    """
    urls = []
    if isinstance(data, dict):
        for _, value in data.items():
            if isinstance(value, (dict, list)):
                urls += find_urls(value)
            elif isinstance(value, str) and _is_url(value):
                urls.append(value)
    elif isinstance(data, list):
        for item in data:
            urls += find_urls(item)
    return urls


def find_name_url_pairs(
    data: Union[Dict[str, Union[str, dict, list]], list],
) -> Dict[str, str]:
    """
    Recursively find all name and URL pairs in a nested dictionary or list,
    specifically looking for 'name' and 'url' keys in 'attachments'.

    Args:
        data (Union[Dict[str, Union[str, dict, list]], list]): The data to search for name-URL pairs.

    Returns:
        Dict[str, str]: A dictionary of name-URL pairs.
    """
    name_url_pairs = {}

    def extract_attachments(attachments: dict):
        """Extract name-URL pairs from attachments."""
        for attachment_value in attachments.values():
            if (
                isinstance(attachment_value, dict)
                and "name" in attachment_value
                and "url" in attachment_value
            ):
                name_url_pairs[attachment_value["name"]] = attachment_value["url"]

    def extract_linked(linked: dict):
        """Extract id-URL pairs from linked items."""
        for linked_value in linked.values():
            if isinstance(linked_value, dict):
                for item_data in linked_value.values():
                    if (
                        isinstance(item_data, dict)
                        and "id" in item_data
                        and "url" in item_data
                    ):
                        name_url_pairs[item_data["id"]] = item_data["url"]

    def recursive_search(data):
        """Recursively search for attachments and linked sections in nested structures."""
        if isinstance(data, dict):
            for key, value in data.items():
                if key == "attachments" and isinstance(value, dict):
                    extract_attachments(value)
                elif key == "linked" and isinstance(value, dict):
                    extract_linked(value)
                elif isinstance(value, (dict, list)):
                    recursive_search(value)
        elif isinstance(data, list):
            for item in data:
                recursive_search(item)

    recursive_search(data)

    return name_url_pairs


# SharePoint/GO illegal filename characters: ~ " # % & * : < > ? \ { | }
# Replace any of these with a dash so GO accepts the filename.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[~"#%&*:<>?\\{|}]')


def _sanitize_filename(name: str) -> str:
    """Replace SharePoint-illegal characters in a filename stem or full name."""
    return _ILLEGAL_FILENAME_CHARS.sub('-', name)


def extract_filename_from_url(url: str) -> str:
    """
    Extract the filename from a given URL.

    Args:
        url (str): The URL to extract the filename from.

    Returns:
        str: The extracted filename, with SharePoint-illegal characters removed.
    """
    parsed_url = urlparse(url)
    path_segments = parsed_url.path.split("/")
    filename = path_segments[-1]
    original_filename = unquote(filename)
    stem, ext = os.path.splitext(original_filename)
    return _sanitize_filename(stem) + ext


def extract_filename_from_url_without_extension(url: str) -> str:
    """
    Extract the filename from a given URL without the extension.

    Args:
        url (str): The URL to extract the filename from.

    Returns:
        str: The extracted filename without extension, with SharePoint-illegal characters removed.
    """
    parsed_url = urlparse(url)
    path_segments = parsed_url.path.split("/")
    filename = path_segments[-1]
    original_filename = unquote(filename)
    filename_without_extension, _ = os.path.splitext(original_filename)
    return _sanitize_filename(filename_without_extension)


def extract_key_value_pairs_from_json(
    json_data, node_name=None, separator=";#", target_type=str
):
    """
    Recursively traverses a JSON object (a dictionary or list) and extracts key-value pairs
    from values that match the specified target type (by default, strings). The key-value pairs
    are extracted from strings using the provided separator. The node to target can be specified
    by name, and the function will find that node anywhere in the structure.

    Parameters:
    -----------
    json_data : dict or list
        The input JSON-like object (nested dictionary or list) to traverse.
    node_name : str, optional
        The name of the node to search for. If None, the function will search the entire JSON
        structure for values that match the target type and contain the separator.
    separator : str, optional
        The separator used in strings to split key-value pairs (default is ";#").
    target_type : type, optional
        The type of values to process for key-value pair extraction. By default, it is `str`,
        so it extracts from strings, but you can specify other types (e.g., list, dict).

    Returns:
    --------
    dict
        A dictionary of extracted key-value pairs from the JSON object.
    """
    result = {}

    def extract_pairs(value):
        """
        Splits a value (usually a string) using the specified separator and creates key-value pairs
        by pairing adjacent items.

        Parameters:
        -----------
        value : str
            The string to be split and processed into key-value pairs.

        Returns:
        --------
        dict
            A dictionary with key-value pairs extracted from the string.
        """
        categories = value.split(separator)
        return {
            categories[i + 1].strip(): categories[i].strip()
            for i in range(0, len(categories) - 1, 2)
        }

    def find_and_extract_from_node(data):
        """
        Recursively traverses the JSON structure to find nodes with the specified name and extracts
        key-value pairs from them if they match the target type.

        Parameters:
        -----------
        data : dict, list, or any type
            The JSON-like structure to traverse and extract key-value pairs from.
        """
        if isinstance(data, dict):
            for key, value in data.items():
                if (
                    key == node_name
                    and isinstance(value, target_type)
                    and separator in str(value)
                ):
                    result.update(extract_pairs(value))
                elif isinstance(value, (dict, list)):
                    find_and_extract_from_node(value)
        elif isinstance(data, list):
            for item in data:
                find_and_extract_from_node(item)

    find_and_extract_from_node(json_data)

    return result


def fetch_cases_metadata(connection_string) -> Dict | None:
    """
    Retrieve metadata for a specific os2formWebformId.

    Args:
        connection_string (str): The connection string for the database

    Returns:
        Dictionary where os2formWebFormId is key and remaining columns from Metadata are values (as dictionaries)"""
    try:
        with pyodbc.connect(connection_string) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    os2formWebformId
                    ,description
                    ,caseType
                    ,spUpdateResponseData
                    ,spUpdateProcessStatus
                    ,caseData
                    ,documentData
                FROM
                    [RPA].[journalizing].[Metadata]
                WHERE
                    [isActive] = 1
                """
            )
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            if len(rows) != 0:
                result = {}
                for row in rows:
                    result[row[0]] = dict(zip(columns[1:], row[1:]))
                return result
            return None

    except pyodbc.Error as e:
        print(f"Database error: {e}")
        return None
    except Exception as e:
        print(f"An error occurred: {e}")
        return None
