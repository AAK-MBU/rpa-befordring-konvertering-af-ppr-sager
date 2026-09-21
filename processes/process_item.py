"""Module to handle item processing"""

import logging

from mbu_rpa_core.exceptions import BusinessError

from processes import bevilling_creation

logger = logging.getLogger(__name__)


def process_item(item_data: dict, item_reference: str):
    """
    Orchestrates the full conversion of one PPR case.

    Flow:
        1. GO conversion  -- fetch PPR docs, resolve/create BOR folder + case,
                             journalize documents into the BOR case.
        2. Bevilling      -- ensure student exists in the befordring app,
                             then create bevilling and koerselsraekke records.

    Expects item_data keys:
        ppr_case_id  (str)        -- PPR source case, e.g. "PPR-2016-123456"
        person_ssn   (str)        -- Citizen SSN, e.g. "010101-1234"
        adresse_id   (str | None) -- AdresseId resolved from LOIS at queue time.
        bevillinger  (list[dict]) -- Bevillinger grouped from BefordringsData.
                                     Each entry has:
                                       bevilling-level fields
                                       koerselsraekker (list[dict])
    """

    ppr_case_id = item_data.get("ppr_case_id", "")
    person_ssn = item_data.get("person_ssn", "")
    adresse_id = item_data.get("adresse_id")
    bevillinger = item_data.get("bevillinger", [])

    if not ppr_case_id:
        raise BusinessError("Item is missing ppr_case_id.")

    if not adresse_id:
        raise BusinessError(
            f"No address could be resolved from LOIS for SSN {person_ssn} "
            f"(PPR case {ppr_case_id}) — needs manual follow-up."
        )

    total_koerselsraekker = sum(len(b.get("koerselsraekker", [])) for b in bevillinger)
    logger.info(
        "Processing PPR case %s | SSN %s | %d bevilling(er) | %d koerselsraekke(r)\n",
        ppr_case_id,
        person_ssn,
        len(bevillinger),
        total_koerselsraekker,
    )

    bor_case_id = ppr_case_id

    # --- Step 2: Bevilling ---
    bevilling_creation.create_bevilling(
        ppr_case_id=ppr_case_id,
        person_ssn=person_ssn,
        bor_case_id=bor_case_id,
        bevillinger=bevillinger,
        adresse_id=adresse_id,
    )
