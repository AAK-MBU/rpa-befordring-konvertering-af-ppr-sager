"""Module to handle item processing"""

import logging

from mbu_rpa_core.exceptions import BusinessError

from processes import bevilling_creation

logger = logging.getLogger(__name__)


def process_item(item_data: dict, item_reference: str):
    """
    Converts one PPR case into bevillinger in the befordring application.

    Expects item_data keys:
        ppr_case_id  (str)        -- PPR source case, e.g. "PPR-2016-123456"
        person_ssn   (str)        -- Citizen SSN, e.g. "010101-1234"
        bevillinger  (list[dict]) -- Bevillinger grouped from BefordringsData.
                                     Each entry has:
                                       bevilling-level fields
                                       adresse_id (str | None), resolved at
                                         queue time against the Adresse table
                                       koerselsraekker (list[dict])
    """

    ppr_case_id = item_data.get("ppr_case_id", "")
    person_ssn = item_data.get("person_ssn", "")
    bevillinger = item_data.get("bevillinger", [])

    if not ppr_case_id:
        raise BusinessError("Item is missing ppr_case_id.")

    # Bevilling.adresse_id is NOT NULL, so a bevilling whose address could not
    # be matched cannot be created. Rejected as a whole case rather than
    # part-converted: a case missing one of its bevillinger looks complete to a
    # caseworker and is harder to spot than one that never arrived.
    unresolved = [
        i for i, bevilling in enumerate(bevillinger, start=1)
        if not bevilling.get("adresse_id")
    ]

    if unresolved:
        raise BusinessError(
            f"PPR case {ppr_case_id} (SSN {person_ssn}): could not match an "
            f"address in the Adresse table for bevilling(er) "
            f"{', '.join(str(i) for i in unresolved)} of {len(bevillinger)} — "
            "needs manual follow-up."
        )

    total_koerselsraekker = sum(len(b.get("koerselsraekker", [])) for b in bevillinger)
    logger.info(
        "Processing PPR case %s | SSN %s | %d bevilling(er) | %d koerselsraekke(r)\n",
        ppr_case_id,
        person_ssn,
        len(bevillinger),
        total_koerselsraekker,
    )

    bevilling_creation.create_bevilling(
        ppr_case_id=ppr_case_id,
        person_ssn=person_ssn,
        bevillinger=bevillinger,
    )
