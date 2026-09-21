"""Module to handle journalisering functionality in GetOrganized."""

from xml.sax.saxutils import escape
from mbu_dev_shared_components.getorganized import api, cases, contacts, objects


class CaseHandler:
    """
    A class to manage the creation of cases in the GetOrganized system.

    Attributes:
    - api_username (str): The username for GetOrganized API.
    - api_password (str): The password for GetOrganized API.
    """

    def __init__(self, api_endpoint: str, api_username: str, api_password: str):
        self.api_username = api_username
        self.api_password = api_password
        self.api_endpoint = api_endpoint
        self.case_obj = objects.CaseDataJson()

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

    def get_case_metadata(self, endpoint_path):
        """
        Function to retrieve metadata for a specified case
        """

        endpoint = self._get_full_endpoint(endpoint_path)

        return cases.get_case_metadata(endpoint, self.api_username, self.api_password)

    def create_case_folder_data(
        self,
        case_type_prefix: objects.CaseTypePrefix,
        person_full_name: str,
        person_id: str,
        person_ssn: str,
        return_when_case_fully_created: bool = True,
    ) -> str:
        """
        Creates JSON data for a case folder.

        Returns:
        - str: JSON string of case folder data.
        """
        xml_case_metadata = (
            '<z:row xmlns:z="#RowsetSchema" '
            'ows_CaseStatus="Åben" '
            f'ows_CaseCategory="Borgermappe" '
            f'ows_CCMContactData="{person_full_name};#{person_id};#{person_ssn};#;#" '
            + "/>"
        )

        return self.case_obj.case_data_json(
            case_type_prefix, xml_case_metadata, return_when_case_fully_created
        )

    def create_ppr_case_folder_data(
        self,
        case_type_prefix: objects.CaseTypePrefix,
        person_full_name: str,
        person_id: str,
        person_ssn: str,
        return_when_case_fully_created: bool = True,
    ) -> str:
        """
        Creates JSON data for a case folder.

        Returns:
        - str: JSON string of case folder data.
        """
        xml_case_metadata = (
            '<z:row xmlns:z="#RowsetSchema" '
            'ows_CaseStatus="Åben" '
            f'ows_CaseCategory="Offentlig" '
            f'ows_CCMContactData="{person_full_name};#{person_id};#{person_ssn};#;#" '
            + "/>"
        )

        return self.case_obj.case_data_json(
            case_type_prefix, xml_case_metadata, return_when_case_fully_created
        )

    def create_case_data(
        self,
        case_type_prefix: objects.CaseTypePrefix,
        case_category: str,
        case_owner_id: str,
        case_owner_name: str,
        case_profile_id: str,
        case_profile_name: str,
        case_title: str,
        case_folder_id: str = None,
        supplementary_case_owners: str = None,
        department_id: str = None,
        department_name: str = None,
        supplementary_departments: str = None,
        kle_number: str = None,
        facet: str = None,
        start_date: str = None,
        special_group: str = None,
        custom_master_case: str = None,
        return_when_case_fully_created: bool = True,
    ) -> str:
        """
        Creates JSON data for a case.

        Returns:
        - str: JSON string of case data.
        """
        xml_metadata = (
            '<z:row xmlns:z="#RowsetSchema" '
            'ows_CaseStatus="Åben" '
            f'ows_CaseCategory="{case_category}" '
            f'ows_Title="{case_title}" '
            f'ows_CaseOwner="{case_owner_id};#{case_owner_name}" '
            f'ows_Afdeling="{department_id};#{department_name}" '
            f'ows_Sagsprofil_{case_type_prefix}="{case_profile_id};#{case_profile_name}" '
            + (
                f'ows_CCMParentCase="{case_folder_id};#{case_type_prefix}" '
                if case_folder_id
                else ""
            )
            + (
                f'ows_SupplerendeSagsbehandlere="{supplementary_case_owners}" '
                if supplementary_case_owners
                else ""
            )
            + (
                f'ows_SupplerendeAfdelinger="{supplementary_departments}" '
                if supplementary_departments
                else ""
            )
            + (f'ows_KLENummer="{kle_number}" ' if kle_number else "")
            + (f'ows_Facet="{facet}" ' if facet else "")
            + (f'ows_Modtaget="{start_date}" ' if start_date else "")
            + (f'ows_SpecialGroup="{special_group}" ' if special_group else "")
            + (
                f'ows_CustomMasterCase="{custom_master_case}" '
                if custom_master_case
                else ""
            )
            + "/>"
        )

        return self.case_obj.case_data_json(
            case_type_prefix, xml_metadata, return_when_case_fully_created
        )

    def create_ppr_case(
        self,
        case_type_prefix: objects.CaseTypePrefix,
        case_category: str,
        case_owner_id: str,
        case_owner_name: str,
        case_profile_id: str,
        case_profile_name: str,
        case_title: str,
        case_folder_id: str = None,
        department_id: str = None,
        department_name: str = None,
        kle_number: str = None,
        facet: str = None,
        start_date: str = None,
        person_full_name: str = None,
        person_go_id: str = None,
        person_ssn: str = None,
        return_when_case_fully_created: bool = True,
    ) -> str:
        """
        Creates JSON data for a case.

        Returns:
        - str: JSON string of case data.
        """
        xml_metadata = (
            '<z:row xmlns:z="#RowsetSchema" '
            'ows_CaseStatus="Åben" '
            f'ows_CaseCategory="{escape(case_category)}" '
            f'ows_Title="{escape(case_title)}" '
            f'ows_CaseOwner="{case_owner_id};#{escape(case_owner_name)}" '
            + (f'ows_Afdeling="{department_id};#{escape(department_name)}" ' if department_id and department_name else "")
            + f'ows_Sagsprofil_{case_type_prefix}="{case_profile_id};#{escape(case_profile_name)}" '
            + (
                f'ows_CCMParentCase="{case_folder_id};#{case_type_prefix}" '
                if case_folder_id
                else ""
            )
            + (
                f'ows_CCMContactData="{escape(person_full_name)};#{person_go_id};#{person_ssn};#;#" '
                if person_full_name and person_go_id and person_ssn
                else ""
            )
            + (f'ows_KLENummer="{escape(kle_number)}" ' if kle_number else "")
            + (f'ows_Facet="{escape(facet)}" ' if facet else "")
            + (f'ows_Modtaget="{escape(start_date)}" ' if start_date else "")
            + "/>"
        )

        return self.case_obj.case_data_json(
            case_type_prefix, xml_metadata, return_when_case_fully_created
        )

    def search_for_case_folder(self, case_folder_search_data: str, endpoint_path: str):
        """
        Search for case folder

        Parameters:
        - case_folder_search_data (str): JSON string of search data.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return cases.find_case_by_case_properties(
            case_folder_search_data, endpoint, self.api_username, self.api_password
        )

    def create_case_folder(self, case_folder_data: str, endpoint_path: str):
        """
        Creates a case in the GetOrganized system using the provided case data.

        Parameters:
        - case_data (str): JSON string of case data.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return cases.create_case_folder(
            case_folder_data, endpoint, self.api_username, self.api_password
        )

    def create_case(self, case_data: str, endpoint_path: str):
        """
        Creates a case in the GetOrganized system using the provided case data.

        Parameters:
        - case_data (str): JSON string of case data.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return cases.create_case(
            case_data, endpoint, self.api_username, self.api_password
        )

    def contact_lookup(self, person_ssn: str, endpoint_path: str):
        """
        Looks up contact information based on a person's social security number (SSN).

        Parameters:
        - person_ssn (str): The social security number of the person.
        - endpoint_path (str): The specific path for the API endpoint.

        Returns:
        - str: JSON string of the contact information.
        """
        endpoint = self._get_full_endpoint(endpoint_path)

        return contacts.contact_lookup(
            person_ssn=person_ssn,
            api_endpoint=endpoint,
            api_username=self.api_username,
            api_password=self.api_password,
        )

    def health_check(self, endpoint_path: str):
        """
        Checks whether GO API is running

        Parameters:
        - endpoint_path (str): The endpoint to check

        Returns:
        - bool: Whether API is running or not
        """

        endpoint = self._get_full_endpoint(endpoint_path)

        api_ready = api.health_check(
            endpoint, api_username=self.api_username, api_password=self.api_password
        )
        return api_ready
