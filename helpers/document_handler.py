"""Module to handle document journalisering functionality in GetOrganized."""

import requests

from mbu_dev_shared_components.getorganized import documents, objects
from mbu_dev_shared_components.getorganized.auth import get_ntlm_go_api_credentials


class DocumentHandler:
    """
    A class to manage the jouranlizing of documents in the GetOrganized system.

    Attributes:
    - api_username (str): The username for GetOrganized API.
    - api_password (str): The password for GetOrganized API.
    """
    def __init__(self, api_endpoint: str, api_username: str, api_password: str):
        self.api_username = api_username
        self.api_password = api_password
        self.api_endpoint = api_endpoint
        self.document_obj = objects.DocumentJsonCreator()

    def _get_full_endpoint(self, path: str):
        """
        Constructs the full endpoint URL.

        Parameters:
        - path (str): The specific path for the API endpoint.

        Returns:
        - str: The full endpoint URL.
        """
        if path:
            return f"{self.api_endpoint}{path}"
        return self.api_endpoint

    def create_document_metadata(self,
                                 case_id: int,
                                 filename: str,
                                 data_in_bytes: bytes,
                                 overwrite: bool,
                                 list_name: str = "Dokumenter",
                                 folder_path: str = "",
                                 document_date: str = "",
                                 document_title: str = "",
                                 document_receiver: str = "",
                                 document_category: str = ""
                                 ):
        """
        Creates JSON data for a document.

        Returns:
        - str: JSON string of document data.
        """
        xml_document_metadata = (
            '<z:row xmlns:z=\"#RowsetSchema\" '
            + (f'ows_Dato=\"{document_date}\" ' if document_date else '')
            + (f'ows_Title=\"{document_title}\" ' if document_title else '')
            + (f'ows_Modtagere=\"{document_receiver}\" ' if document_receiver else '')
            + (f'ows_Korrespondance=\"{document_category}\" ' if document_category else '')
            + '/>'
        )

        return self.document_obj.document_data_json(case_id, list_name, folder_path, filename, xml_document_metadata, overwrite, data_in_bytes)

    def upload_document(self, document_data: str, endpoint_path: str):
        """
        Adds new document to case using XML metadata.

        Parameters:
        - document metadata (str): A JSON string containing file data of the document to be uploaded.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return documents.upload_file_to_case(document_data, endpoint, self.api_username, self.api_password)

    def journalize_document(self, document_ids: list, endpoint_path: str):
        """
        Marks document as Case Record.

        Parameters:
        - document_ids (list): List of ids on the documents to journalize.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return documents.mark_file_as_case_record(document_ids, endpoint, self.api_username, self.api_password)

    def finalize_document(self, document_ids: list, endpoint_path: str):
        """
        Marks document as Finalized.

        Parameters:
        - document_ids (list): List of ids on the documents to finalize.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return documents.finalize_file(document_ids, endpoint, self.api_username, self.api_password)

    def download_document(self, file_url: str) -> bytes:
        """
        Downloads a document from GetOrganized by its full URL.

        Parameters:
        - file_url (str): The full URL to the file (typically the 'path' field from search results).

        Returns:
        - bytes: The raw file content.
        """
        response = requests.get(
            file_url,
            auth=get_ntlm_go_api_credentials(self.api_username, self.api_password),
            timeout=60,
        )
        response.raise_for_status()
        return response.content

    def search_documents_using_search_term(self, search_term, endpoint_path):
        """
        Search for all documents related to a specified search_term
        """

        endpoint = self._get_full_endpoint(endpoint_path)

        return documents.search_documents(search_term, endpoint, self.api_username, self.api_password)
