"""
This module handles the journalization process for case management.
It contains functionality to upload and journalize documents, and manage case data.
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pyodbc
from dateutil.relativedelta import relativedelta
from mbu_dev_shared_components.database.connection import RPAConnection
from mbu_dev_shared_components.getorganized.objects import CaseDataJson
from mbu_dev_shared_components.os2forms.documents import download_file_bytes
from mbu_dev_shared_components.utils.db_stored_procedure_executor import (
    execute_stored_procedure,
)

from case_manager.case_handler import CaseHandler
from case_manager.document_handler import DocumentHandler
from case_manager.helper_functions import (
    extract_filename_from_url,
    extract_filename_from_url_without_extension,
    extract_key_value_pairs_from_json,
    find_name_url_pairs,
    notify_stakeholders,
)
from config import (
    FORM_RETRY_WAIT_CONTACT_LOOKUP_MINUTES,
    FORM_RETRY_WAIT_HEALTH_CHECK_MINUTES,
)


class DatabaseError(Exception):
    """Custom exception for database related errors."""


class RequestError(Exception):
    """Custom exception for request related errors."""


class EnvVarNotFoundError(Exception):
    """Custom exception for environment variable not found."""

    def __init__(self, var_name, message="Environment variable not found"):
        self.var_name = var_name
        self.message = f"{message}: {var_name}"
        super().__init__(self.message)


def execute_sql_update(
    conn_string: str, procedure_name: str, params: Dict[str, tuple]
) -> None:
    """
    Execute a stored procedure to update data in the database.

    Args:
        conn_string (str): Connection string for the database.
        procedure_name (str): Name of the stored procedure to execute.
        params (Dict[str, tuple]):
            Parameters for the SQL procedure, in the form {param_name: (param_type, param_value)}.

    Raises:
        DatabaseError: If the SQL procedure execution fails.
    """
    sql_update_result = execute_stored_procedure(conn_string, procedure_name, params)
    if not sql_update_result["success"]:
        raise DatabaseError(f"SQL - {procedure_name} failed.")


def log_and_raise_error(
    log_db: str,
    error_message: str,
    context: str,
    exception: Exception,
    db_env: str,
) -> None:
    """
    Log an error and raise the specified exception.

    Args:
        orchestrator_connection (OrchestratorConnection): Connection object to log errors.
        error_message (str): The error message to log.
        exception (Exception): The exception to raise.

    Raises:
        exception: The passed-in exception is raised after logging the error.
    """
    with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
        rpa_conn.log_event(
            log_db=log_db,
            level="ERROR",
            message=error_message,
            context=context,
        )
    raise exception


def handle_database_error(
    conn_string: str,
    procedure_name: str,
    process_status_params_failed: str,
    exception: Exception,
) -> None:
    """
    Handle database errors by executing the failure procedure and raising the exception.

    Args:
        conn_string (str): Connection string for the database.
        procedure_name (str): Name of the stored procedure to execute upon failure.
        process_status_params_failed (str): Parameters for the failure procedure.
        exception (Exception): The exception that occurred.

    Raises:
        exception: Re-raises the original exception.
    """
    execute_stored_procedure(conn_string, procedure_name, process_status_params_failed)
    raise exception


def get_forms_data(
    conn_string: str, params: Optional[List[Any]] = None
) -> List[Dict[str, Any]]:
    """Retrieve the data for the specific form from the database."""
    try:
        query = f"""
            SELECT
                j.form_id
                ,f.form_data
                ,CAST(f.form_submitted_date AS datetime) AS form_submitted_date
                ,f.form_type as os2formwebform_id,
                j.attempt_count
            FROM
                [RPA].[journalizing].[Journalizing] j
            JOIN
                [RPA].[journalizing].[Forms] f on f.form_id = j.form_id
            WHERE  
                (
                    j.status = 'New'
                    OR
                    (
                        j.status = 'Failed'
                        AND j.attempt_count IS NOT NULL
                        AND j.attempt_count < 3
                        AND j.response NOT LIKE '%"Case":{{"CaseId": "%"}}%'
                        AND j.response LIKE '%"HealthCheck":{{"Successful": "False"}}%'
                        AND j.last_time_modified < DATEADD(MINUTE, -{FORM_RETRY_WAIT_HEALTH_CHECK_MINUTES}, GETDATE())
                    )
                    OR
                    (
                        j.status = 'Failed'
                        AND j.attempt_count IS NOT NULL
                        AND j.attempt_count < 3
                        AND j.response NOT LIKE '%"Case":{{"CaseId": "%"}}%'
                        AND j.response LIKE '%"HealthCheck":{{"Successful": "True"}}%'
                        AND j.last_time_modified < DATEADD(MINUTE, -{FORM_RETRY_WAIT_CONTACT_LOOKUP_MINUTES}, GETDATE())
                    )
                )
            AND
                f.form_type in
                    ({", ".join(["?" for _ in range(len(params))])})  -- returns (?, ?, ...) for number of params
            ORDER BY
                f.form_submitted_date ASC
        """
        with pyodbc.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                form_list = [f"{i}" for i in params]
                cursor.execute(query, form_list)
                columns = [column[0] for column in cursor.description]
                forms_data = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return forms_data
    except pyodbc.Error as e:
        raise SystemExit(e) from e


def get_credentials_and_constants(db_env="PROD") -> Dict[str, Any]:
    """Retrieve necessary credentials from system environment variables"""
    try:
        with RPAConnection(db_env=db_env) as rpa_conn:
            credentials = {
                "go_api_endpoint": rpa_conn.get_constant("go_api_endpoint")["value"],
                "go_api_username": rpa_conn.get_credential("go_api")["username"],
                "go_api_password": rpa_conn.get_credential("go_api")[
                    "decrypted_password"
                ],
                "os2_api_key": rpa_conn.get_credential("os2_api")["decrypted_password"],
                "DbConnectionString": rpa_conn.get_constant("DbConnectionString")[
                    "value"
                ],
                "journalizing_tmp_path": rpa_conn.get_constant("journalizing_tmp_path")[
                    "value"
                ],
            }
        return credentials
    except AttributeError as e:
        raise SystemExit(e) from e


def health_check(
    case_handler: CaseHandler,
    conn_string: str,
    form_id: str,
    update_response_data: str,
    update_process_status: str,
    process_status_params_failed: str,
):
    """
    Perform health_check of GetOrganized API
    """
    api_ok = False
    try:
        api_ok = case_handler.health_check("/_api/web")

        if not api_ok:
            raise RequestError("Api health check failed")
        # SQL data update
        sql_data_params = {
            "StepName": ("str", "HealthCheck"),
            "JsonFragment": ("str", json.dumps({"Successful": str(api_ok)})),
            "form_id": ("str", form_id),
        }
        execute_sql_update(conn_string, update_response_data, sql_data_params)

    except (RequestError, DatabaseError) as e:
        handle_database_error(
            conn_string=conn_string,
            procedure_name=update_process_status,
            process_status_params_failed=process_status_params_failed,
            exception=e,
        )

    except Exception as e:
        handle_database_error(
            conn_string=conn_string,
            procedure_name=update_process_status,
            process_status_params_failed=process_status_params_failed,
            exception=RuntimeError(
                f"An unexpected error occured during API health check: {e}"
            ),
        )

    return api_ok


def contact_lookup(
    case_handler: CaseHandler,
    ssn: str,
    conn_string: str,
    update_response_data: str,
    update_process_status: str,
    process_status_params_failed: str,
    form_id: str,
) -> Optional[Tuple[str, str]]:
    """
    Perform contact lookup and update the database with the contact information.

    Returns:
        A tuple containing the person's full name and ID if successful, otherwise None.
    """
    try:
        response = case_handler.contact_lookup(
            ssn, "/borgersager/_goapi/contacts/readitem"
        )
        if not response.ok:
            raise RequestError("Request response failed during contact lookup.")

        person_data = response.json()
        person_full_name = person_data["FullName"]
        person_go_id = person_data["ID"]

        # SQL data update
        sql_data_params = {
            "StepName": ("str", "ContactLookup"),
            "JsonFragment": ("str", json.dumps({"ContactId": person_go_id})),
            "form_id": ("str", form_id),
        }
        execute_sql_update(conn_string, update_response_data, sql_data_params)

        return person_full_name, person_go_id

    except (DatabaseError, RequestError) as e:
        handle_database_error(
            conn_string, update_process_status, process_status_params_failed, e
        )
        return None

    except Exception as e:
        handle_database_error(
            conn_string,
            update_process_status,
            process_status_params_failed,
            RuntimeError(f"An unexpected error occurred during contact lookup: {e}"),
        )
        return None


def check_case_folder(
    case_handler: CaseHandler,
    case_data_handler: CaseDataJson,
    case_type: str,
    person_full_name: str,
    person_go_id: str,
    ssn: str,
    conn_string: str,
    update_response_data: str,
    update_process_status: str,
    process_status_params_failed: str,
    form_id: str,
    db_env: str,
) -> Optional[str]:
    """
    Check if a case folder exists for the person and update the database.

    Returns:
        The case folder ID if it exists, otherwise None.
    """
    try:
        search_data = case_data_handler.search_citizen_folder_data_json(
            case_type, person_full_name, person_go_id, ssn
        )
        response = case_handler.search_for_case_folder(
            search_data, "/_goapi/cases/findbycaseproperties"
        )

        if not response.ok:
            raise RequestError("Request response failed during check case folder.")

        cases_info = response.json().get("CasesInfo", [])
        case_folder_id = cases_info[0].get("CaseID") if cases_info else None

        if case_folder_id is None:
            field_properties = {"ows_CCMContactData_CPR": ssn}

            search_data = case_data_handler.generic_search_case_data_json(
                case_type,
                person_full_name,
                person_go_id,
                ssn,
                include_name=True,
                returned_cases_number="25",
                field_properties=field_properties,
            )

            response = case_handler.search_for_case_folder(
                search_data, "/_goapi/cases/findbycaseproperties"
            )

            res_json = response.json()

            pattern = re.compile(r"^BOR-\d{4}-\d{6}$")

            for row in res_json.get("CasesInfo", []):
                if pattern.match(row.get("CaseID", "")):
                    case_folder_id = row.get("CaseID")

                    case_url = "https://go.aarhuskommune.dk" + row.get("RelativeUrl")

                    # if we find a case folder, that matches the pattern, it means the citizen has a citizen folder, but it is NOT set with the correct caseCategory (Borgermappe)
                    email_body = (
                        f"<p>Journaliseringsrobot har fanget en borgermappe som er oprettet med forkert caseCategory.</p>"
                        f"<p>"
                        f"<strong>Case URL: {case_url}</strong><br>"
                        f"</p>"
                        f"<strong>Journaliseringen af sag er successfuld - ræk ud til GO-team for at få rettet borgermappe<br>"
                        f"</p>"
                        f"<p>Husk at slette denne mail efter GO-team er notificeret!</p>"
                        f"</p>"
                    )

                    with RPAConnection(db_env=db_env) as rpa_conn:
                        receiver = rpa_conn.get_constant("Error Email")["value"]
                        sender = rpa_conn.get_constant("e-mail_noreply")["value"]
                        smtp_server = rpa_conn.get_constant("smtp_server")["value"]
                        smtp_port = rpa_conn.get_constant("smtp_port")["value"]

                    smtp_util.send_email(
                        receiver=receiver,
                        sender=sender,
                        subject="Borgermappe oprettet forkert, caseCategory er IKKE 'Borgermappe'",
                        body=email_body,
                        html_body=email_body,
                        smtp_server=smtp_server,
                        smtp_port=smtp_port,
                        attachments=None,
                    )

                    break

        if case_folder_id:
            sql_data_params = {
                "StepName": ("str", "CaseFolder"),
                "JsonFragment": ("str", json.dumps({"CaseFolderId": case_folder_id})),
                "form_id": ("str", form_id),
            }
            execute_sql_update(conn_string, update_response_data, sql_data_params)

        return case_folder_id

    except (DatabaseError, RequestError) as e:
        handle_database_error(
            conn_string, update_process_status, process_status_params_failed, e
        )
        return None

    except Exception as e:
        handle_database_error(
            conn_string,
            update_process_status,
            process_status_params_failed,
            RuntimeError(f"An unexpected error occurred during case folder check: {e}"),
        )
        return None


def create_case_folder(
    case_handler: CaseHandler,
    case_type: str,
    person_full_name: str,
    person_go_id: str,
    ssn: str,
    conn_string: str,
    update_response_data: str,
    update_process_status: str,
    process_status_params_failed: str,
    form_id: str,
) -> Optional[str]:
    """
    Create a new case folder if it doesn't exist.

    Returns:
        Optional[str]: The case folder ID if created successfully, otherwise None in case of an error.
    """
    try:
        case_folder_data = case_handler.create_case_folder_data(
            case_type, person_full_name, person_go_id, ssn
        )
        response = case_handler.create_case_folder(case_folder_data, "/_goapi/Cases")
        if not response.ok:
            raise RequestError("Request response failed during create case folder.")

        case_folder_id = response.json()["CaseID"]

        sql_data_params = {
            "StepName": ("str", "CaseFolder"),
            "JsonFragment": ("str", json.dumps({"CaseFolderId": case_folder_id})),
            "form_id": ("str", form_id),
        }
        execute_sql_update(conn_string, update_response_data, sql_data_params)

        return case_folder_id

    except (DatabaseError, RequestError) as e:
        handle_database_error(
            conn_string, update_process_status, process_status_params_failed, e
        )
        return None

    except Exception as e:
        handle_database_error(
            conn_string,
            update_process_status,
            process_status_params_failed,
            RuntimeError(
                f"An unexpected error occurred during case folder creation: {e}"
            ),
        )
        return None


def create_case_data(
    case_handler: CaseHandler,
    case_type: str,
    case_data: Dict[str, Any],
    case_title: str,
    case_folder_id: str,
    received_date: str,
    case_profile_id,
    case_profile_name,
) -> Dict[str, Any]:
    """Create the data needed to create a new case."""
    return case_handler.create_case_data(
        case_type,
        case_data["caseCategory"],
        case_data["caseOwnerId"],
        case_data["caseOwnerName"],
        case_profile_id,
        case_profile_name,
        case_title,
        case_folder_id,
        case_data["supplementaryCaseOwners"],
        case_data["departmentId"],
        case_data["departmentName"],
        case_data["supplementaryDepartments"],
        case_data["kleNumber"],
        case_data["facet"],
        received_date or case_data.get("startDate"),
        case_data["specialGroup"],
        case_data["customMasterCase"],
        True,
    )


def determine_case_title(
    os2form_webform_id: str,
    person_full_name: str,
    ssn: str,
    parsed_form_data,
    meta_case_title,
) -> str:
    """Determine the title of the case based on the webform ID."""

    if os2form_webform_id not in (
        "indmeld_kraenkelser_af_boern",
        "respekt_for_graenser_privat",
        "respekt_for_graenser",
    ):
        placeholder_replacements = [
            ("placeholder_ssn_first_6", ssn[:6]),
            ("placeholder_ssn", ssn),
            ("placeholder_person_full_name", person_full_name),
        ]

        for placeholder, value in placeholder_replacements:
            meta_case_title = meta_case_title.replace(placeholder, value)

        return meta_case_title

    # The part below only handles "indmeld_kraenkelser_af_boern", "respekt_for_graenser_privat", "respekt_for_graenser"
    omraade = parsed_form_data["data"]["omraade"]
    if omraade == "Skole":
        department = parsed_form_data["data"].get("skole", "Ukendt skole")
    elif omraade == "Dagtilbud":
        department = parsed_form_data["data"].get("dagtilbud")
        if not department:
            department = parsed_form_data["data"].get(
                "daginstitution_udv_", "Ukendt dagtilbud"
            )
    elif omraade == "Ungdomsskole":
        department = parsed_form_data["data"].get("ungdomsskole", "Ukendt ungdomsskole")
    elif omraade == "Klub":
        department = parsed_form_data["data"].get("klub", "Ukendt klub")
    else:
        department = "Ukendt afdeling"  # Default if no match
    part_title = None
    if os2form_webform_id == "indmeld_kraenkelser_af_boern":
        part_title = "Forældre/pårørendehenvendelse"
    elif os2form_webform_id == "respekt_for_graenser_privat":
        part_title = "Privat skole/privat dagtilbud-henvendelse"
    elif os2form_webform_id == "respekt_for_graenser":
        part_title = "BU-henvendelse"
    return f"{department} - {part_title}"


def determine_case_profile_id(case_profile_name: str) -> str:
    """Determine the case profile ID based on the case profile name."""
    try:
        credentials = get_credentials_and_constants()
        conn_string = credentials["DbConnectionString"]

        with pyodbc.connect(conn_string) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT case_profile_id FROM [RPA].[rpa].GO_CaseProfiles_View WHERE name like ?",
                case_profile_name,
            )
            row = cursor.fetchone()
            if row:
                return row[0]

        return None

    except pyodbc.Error as e:
        print(f"Database error: {e}")
        return None

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


def determine_case_profile(
    os2form_webform_id, case_data, parsed_form_data
) -> Tuple[str, str]:
    """Determine the case profile ID and name."""

    # If the case profile ID and name are provided in the JSON arguments, use them
    if case_data["caseProfileId"] != "" and case_data["caseProfileName"] != "":
        return case_data["caseProfileId"], case_data["caseProfileName"]

    # Determine the case profile based on the webform ID
    match os2form_webform_id:
        case (
            "indmeld_kraenkelser_af_boern"
            | "respekt_for_graenser_privat"
            | "respekt_for_graenser"
        ):
            omraade = parsed_form_data["data"]["omraade"]
            if omraade == "Skole":
                case_profile_name = "MBU PPR Respekt for grænser Skole"
            elif omraade == "Dagtilbud":
                case_profile_name = "MBU PPR Respekt for grænser Dagtilbud"
            elif omraade in {"Ungdomsskole", "Klub"}:  # Merge comparisons using 'in'
                case_profile_name = "MBU PPR Respekt for grænser UngiAarhus"
            else:
                case_profile_name = "MBU PPR Respekt for grænser Skole"  # Default case profile name if none match

    case_profile_id = determine_case_profile_id(case_profile_name)

    return case_profile_id, case_profile_name


def create_case(
    case_handler: CaseHandler,
    parsed_form_data: Dict[str, Any],
    os2form_webform_id: str,
    case_type: str,
    case_data: str,
    conn_string: str,
    update_response_data: str,
    update_process_status: str,
    process_status_params_failed: str,
    form_id: str,
    ssn: str = None,
    person_full_name: str = None,
    case_folder_id: str = None,
    received_date: str = None,
) -> Optional[str]:
    """
    Create a new case and update the database.

    Returns:
        Optional[str]:  The case ID, case title, and relative URL if created successfully,
                        otherwise None in case of an error.
    """
    try:
        case_title = determine_case_title(
            os2form_webform_id,
            person_full_name,
            ssn,
            parsed_form_data,
            case_data.get("meta_case_title", ""),
        )  # meta_case_title needs to be in original case_metadata
        case_data["caseProfileId"], case_data["caseProfileName"] = (
            determine_case_profile(os2form_webform_id, case_data, parsed_form_data)
        )
        created_case_data = create_case_data(
            case_handler,
            case_type,
            case_data,
            case_title,
            case_folder_id,
            received_date,
            case_data["caseProfileId"],
            case_data["caseProfileName"],
        )
        print(created_case_data)
        response = case_handler.create_case(created_case_data, "/_goapi/Cases")
        if not response.ok:
            print(f"Error creating case: {response.status_code} - {response.text}")
            raise RequestError("Request response failed.")

        case_id = response.json()["CaseID"]
        case_rel_url = response.json()["CaseRelativeUrl"]

        sql_data_params = {
            "StepName": ("str", "Case"),
            "JsonFragment": ("str", json.dumps({"CaseId": case_id})),
            "form_id": ("str", form_id),
        }
        execute_sql_update(conn_string, update_response_data, sql_data_params)
        return case_id, case_title, case_rel_url

    except (DatabaseError, RequestError) as e:
        handle_database_error(
            conn_string, update_process_status, process_status_params_failed, e
        )
        print(f"An error occurred: {e}")
        raise e

    except Exception as e:
        handle_database_error(
            conn_string,
            update_process_status,
            process_status_params_failed,
            RuntimeError(f"An unexpected error occurred during case creation: {e}"),
        )
        print(f"An error occurred: {e}")
        raise e


def journalize_file(
    document_handler: DocumentHandler,
    case_id: str,
    case_title,
    case_rel_url: str,
    parsed_form_data: Dict[str, Any],
    os2_api_key: str,
    conn_string: str,
    process_status_params_failed: str,
    form_id: str,
    case_metadata: str,
    log_db: str,
    context: str,
    db_env: str,
    filename_appendage: str = "",
) -> None:
    """Journalize associated files in the 'Document' folder under the citizen case."""

    def upload_single_document(url, received_date, document_category, wait_sec=5):
        """N/A"""
        filename = extract_filename_from_url(url)
        filename_without_extension = extract_filename_from_url_without_extension(url)

        if filename_appendage != "":
            name, ext = os.path.splitext(filename)

            filename = f"{name}{filename_appendage}{ext}"

            filename_without_extension = (
                f"{filename_without_extension}{filename_appendage}"
            )

        print(f"DEBUG upload_single_document:")
        print(f"  case_id          : {case_id!r}")
        print(f"  filename         : {filename!r}")
        print(f"  document_title   : {filename_without_extension!r}")
        print(f"  document_date    : {received_date!r}")
        print(f"  document_category: {document_category!r}")
        print(f"  url              : {url!r}")
        file_bytes = download_file_bytes(url, os2_api_key)
        print(f"  downloaded bytes : {len(file_bytes)}")
        upload_status = "failed"
        upload_attempts = 0

        while upload_status == "failed" and upload_attempts < 5:
            document_data = document_handler.create_document_metadata(
                case_id=case_id,
                filename=filename,
                data_in_bytes=list(file_bytes),
                document_date=received_date,
                document_title=filename_without_extension,
                document_receiver="",
                document_category=document_category,
                overwrite="true",
            )

            if upload_attempts == 0:
                # Print payload structure on first attempt (skip the raw byte array)
                if isinstance(document_data, dict):
                    print(f"DEBUG document payload (no bytes):")
                    for k, v in document_data.items():
                        if isinstance(v, (list, bytes)) and len(v) > 20:
                            print(f"  {k}: <{len(v)} items>")
                        else:
                            print(f"  {k}: {v!r}")
                else:
                    print(f"DEBUG document payload type={type(document_data)}: {str(document_data)[:500]}")

            response = document_handler.upload_document(
                document_data, "/_goapi/Documents/AddToCase"
            )

            upload_attempts += 1
            if response.ok:
                upload_status = "succeeded"
            else:
                print(f"DEBUG upload attempt {upload_attempts} failed: {response.status_code} — {response.text[:500]}")
                time.sleep(wait_sec)

        attempts_string = f"{upload_attempts} attempt"
        attempts_string += "s" if upload_attempts > 1 else ""
        with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
            rpa_conn.log_event(
                log_db=log_db,
                level="INFO",
                message=f"Uploading {filename} {upload_status} after {attempts_string}",
                context=context,
            )

        if not response.ok:
            log_and_raise_error(
                log_db=log_db,
                error_message="An error occurred when uploading the document.",
                context=context,
                exception=RequestError("Request response failed."),
                db_env=db_env,
            )

        document_id = response.json()["DocId"]
        with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
            rpa_conn.log_event(
                log_db=log_db,
                level="INFO",
                message=f"Document uploaded with ID: {document_id}",
                context=context,
            )
        return {"DocumentId": str(document_id)}, document_id, file_bytes

    def process_documents(wait_sec=3):
        """N/A"""
        urls = find_name_url_pairs(parsed_form_data)
        document_category_json = extract_key_value_pairs_from_json(
            document_data, node_name="documentCategory"
        )
        received_date = (
            parsed_form_data["entity"]["completed"][0]["value"]
            if document_data["useCompletedDateFromFormAsDate"] == "True"
            else ""
        )

        # If only one category is configured and no key matches the document
        # name, use that single category as a catch-all (avoids the need to
        # keep DB keys in sync with OS2forms filenames when all documents share
        # the same category).
        single_category_fallback = (
            next(iter(document_category_json.values()))
            if len(document_category_json) == 1
            else "Indgående"
        )

        print(f"DEBUG category lookup dict: {document_category_json}")
        print(f"DEBUG document names from form: {list(urls.keys())}")

        documents, document_ids = [], []
        for name, url in urls.items():
            document_category = document_category_json.get(name, single_category_fallback)
            print(f"DEBUG  name={name!r} -> category={document_category!r} (matched={name in document_category_json})")
            doc, doc_id, file_bytes = upload_single_document(
                url, received_date, document_category
            )
            documents.append(doc)
            document_ids.append(doc_id)
            time.sleep(wait_sec)

        return documents, document_ids, file_bytes

    def handle_journalization(document_ids, file_bytes):
        if document_data.get("journalizeDocuments") == "True":
            with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
                rpa_conn.log_event(
                    log_db=log_db,
                    level="INFO",
                    message="Journalizing document.",
                    context=context,
                )
            response = document_handler.journalize_document(
                document_ids, "/_goapi/Documents/MarkMultipleAsCaseRecord/ByDocumentId"
            )
            if not response.ok:
                log_and_raise_error(
                    log_db=log_db,
                    error_message="An error occurred while journalizing the document.",
                    context=context,
                    exception=RequestError("Request response failed."),
                    db_env=db_env,
                )
            with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
                rpa_conn.log_event(
                    log_db=log_db,
                    level="INFO",
                    message="Document was journalized.",
                    context=context,
                )
            print("Attempting notification")
            notify_stakeholders(
                case_metadata,
                case_id,
                case_title,
                case_rel_url,
                False,
                file_bytes,
                db_env=db_env,
            )

    def handle_finalization(document_ids):
        if document_data.get("finalizeDocuments") == "True":
            with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
                rpa_conn.log_event(
                    log_db=log_db,
                    level="INFO",
                    message="Finalizing document.",
                    context=context,
                )
            response = document_handler.finalize_document(
                document_ids, "/_goapi/Documents/FinalizeMultiple/ByDocumentId"
            )
            if not response.ok:
                log_and_raise_error(
                    log_db=log_db,
                    error_message="An error occurred while finalizing the document.",
                    context=context,
                    exception=RequestError("Request response failed."),
                    db_env=db_env,
                )
            with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
                rpa_conn.log_event(
                    log_db=log_db,
                    level="INFO",
                    message="Document was finalized.",
                    context=context,
                )

    try:
        with RPAConnection(db_env=db_env, commit=True) as rpa_conn:
            rpa_conn.log_event(
                log_db=log_db,
                level="INFO",
                message="Uploading document(s) to the case.",
                context=context,
            )
        document_data = json.loads(case_metadata["documentData"])
        documents, document_ids, file_bytes = process_documents()

        sql_data_params = {
            "StepName": ("str", "Case Files"),
            "JsonFragment": ("str", json.dumps(documents)),
            "form_id": ("str", form_id),
        }
        execute_sql_update(
            conn_string, case_metadata["spUpdateResponseData"], sql_data_params
        )

        handle_journalization(document_ids, file_bytes)
        handle_finalization(document_ids)

    except (DatabaseError, RequestError) as e:
        print(f"An error occurred: {e}")
        handle_database_error(
            conn_string,
            case_metadata["spUpdateProcessStatus"],
            process_status_params_failed,
            e,
        )

    except Exception as e:
        print(f"An unexpected error occurred during file journalization: {e}")
        handle_database_error(
            conn_string,
            case_metadata["spUpdateProcessStatus"],
            process_status_params_failed,
            RuntimeError(
                f"An unexpected error occurred during file journalization: {e}"
            ),
        )


def look_for_existing_modtagelsesklasse_case(case_handler: CaseHandler, document_handler: DocumentHandler, ssn):
    """
    A function to look for an existing citizen case for a specified form type
    """

    case_id = ""
    case_title = ""
    case_relative_url = ""

    filename_appendage = ""

    keyword = "Kvitteringmodtagelsesklasse"

    max_suffix = 2

    cutoff_date = date.today() - relativedelta(months=3)

    response = document_handler.search_documents_using_search_term(
        f"{ssn} {keyword}", "/_goapi/Search/Results"
    )

    if not response.ok:
        raise Exception("Request response failed.")

    res_rows = response.json()["Rows"].get("Results", [])

    res_rows.sort(key=lambda x: x.get("created", ""), reverse=True)

    # Look for a recently created matching case
    for row in res_rows:
        if keyword in row.get("title", "") and row.get("created"):
            created_date = datetime.fromisoformat(row["created"]).date()

            doc_title = row.get("title", "")

            if created_date >= cutoff_date:
                match = re.match(rf"^{keyword}_(\d+)$", doc_title)

                if match:
                    suffix_num = int(match.group(1))

                    max_suffix = suffix_num + 1

                case_id = row.get("caseid", "")

                case_metadata_response = case_handler.get_case_metadata(
                    f"/_goapi/Cases/Metadata/{case_id}"
                )

                parsed_metadata = ET.fromstring(
                    case_metadata_response.json().get("Metadata")
                ).attrib

                case_title = parsed_metadata.get("ows_Title", "")

                case_relative_url = parsed_metadata.get("ows_CaseUrl", "")

                break  # Stop after first valid match

    if case_id != "" and case_title != "" and case_relative_url != "":
        filename_appendage = f"_{max_suffix}"

    return case_id, case_title, case_relative_url, filename_appendage
