import asyncio
import logging
import sys

from datetime import datetime, timedelta

# from dateutil.relativedelta import relativedelta
from automation_server_client import (
    AutomationServer,
    Workqueue,
    WorkItemError,
    Credential,
)
from kmd_nexus_client import (
    NexusClientManager,    
)
from kmd_nexus_client.tree_helpers import (
    filter_by_path,    
)

from odk_tools.tracking import Tracker
from odk_tools.reporting import Reporter

nexus: NexusClientManager
tracker: Tracker
reporter: Reporter

process_name = "Oprydning af hjælpemiddelsindsatser"
logger = logging.getLogger(process_name)


def populate_queue(workqueue: Workqueue):
    organisation = nexus.organisationer.hent_organisation_ved_navn("Hjælpemiddelservice")
    borgere = nexus.organisationer.hent_borgere_for_organisation(organisation)

    for borger in borgere:
        try:
            cpr = borger["patientIdentifier"]["identifier"]
            workqueue.add_item({}, cpr)
        except Exception:
            continue


async def process_workqueue(workqueue: Workqueue):
    for item in workqueue:
        with item:

            try:
                borger = nexus.borgere.hent_borger(item.reference)

                if borger is None:                    
                    continue

                # Kontrollerer om borgeren har udlån uden indsats
                if kontroller_udlån_uden_indsats(borger):
                    reporter.report(
                        process_name,
                        "Borgere med udlån uden indsats",
                        {"Cpr": borger["patientIdentifier"]["identifier"]},
                    )

                afslut_indsatser(borger)
                fjern_organisations_relation_fra_borger(borger)
            except ValueError as e:
                logger.error(f"Error getting citizen data: {e}")
                item.fail("Ugyldigt cpr-nummer")
            except WorkItemError as e:
                item.fail(str(e))


def kontroller_udlån_uden_indsats(borger: dict) -> bool:
    """
    Kontrollerer om borgeren har udlån uden en tilknyttet indsats.
    param borger: dict - Borgerens data.
    return: bool - True hvis der er udlån uden indsats, False ellers.
    """
    fundet_udlån_uden_indsats = False
    udlån = nexus.borgere.hent_udlån(borger)
    godkendte_status_for_udlån_uden_indsats = [
        "TO_BE_REPAIRED",
        "SENT_TO_DEPOT",
        "TO_BE_TAKEN_HOME",
    ]

    if udlån is None:
        return fundet_udlån_uden_indsats

    for enkelt_udlån in udlån:
        if enkelt_udlån["status"] in godkendte_status_for_udlån_uden_indsats:
            continue
        
        if (
            "grant" not in enkelt_udlån
            or enkelt_udlån["grant"]["originatorStatus"] == "Afsluttet"
            or enkelt_udlån["grant"] is None
        ):
            logging.info(f"Lending found without basket grant: {enkelt_udlån}")
            fundet_udlån_uden_indsats = True

    return fundet_udlån_uden_indsats


def afslut_indsatser(borger: dict):
    """
    Afslutter indsats for en borger, hvis omstændighederne er opfyldt.
    param citizen: dict - Borgerens data.
    """
    pathway = nexus.borgere.hent_visning(borger)
    indsats_referencer = nexus.borgere.hent_referencer(pathway)
    
    filtrerede_indsats_referencer = filter_by_path(
        indsats_referencer,
        path_pattern="/Sundhedsfagligt grundforløb/*/Indsatser/basketGrantReference",
        active_pathways_only=False,
    )

    for reference in filtrerede_indsats_referencer:
        indsats = nexus.hent_fra_reference(reference)

        if indsats["workflowState"]["name"] in [
            "Afsluttet",
            "Annulleret",
            "Afslået",
        ]:
            continue

        indsats_elementer = nexus.indsats.hent_indsats_elementer(indsats)

        # Store variationer i elements gør det svært at checke korrekt
        try:
            name = indsats_elementer["supplier"]["supplier"]["name"]
            if name != "Hjælpemiddelservice":
                continue
        except Exception:
            continue

        if indsats["workflowState"]["name"] == "Bevilliget":
            # Hvis udlånet er indenfor de sidste 31 dage, så skal det ikke afsluttes. Bemærk dato fr nexus har timezone info.
            if indsats_elementer["workflowRequestedDate"] > datetime.now().astimezone() - timedelta(days=31):
                logging.warning(
                    f"Potentielt aktivt udlån på borger {borger['patientIdentifier']['identifier']}"
                )
                continue

        if aktive_udlån(borger, indsats):
            continue

        indsats_slut_dato = indsats_elementer.get("basketGrantEndDate")

        if indsats_slut_dato:
            if indsats_slut_dato > datetime.now().astimezone():
                continue

        logger.info(
            f"Forsøger at redigere indsats med id: {indsats['basketGrantId']} for borger: {borger['patientIdentifier']['identifier']}"
        )

        afslut_indsats(borger, indsats)
        tracker.track_task(process_name)


def aktive_udlån(borger: dict, indsats: dict) -> bool:
    """
    Kontrollerer om der er aktive udlån, der matcher indsatsen.
    param borger: dict - Borgeren.
    param indsats: dict - Indsatsen.
    return: bool - True hvis der er aktive udlån, der matcher indsatsen, ellers False.
    """
    udlån = nexus.borgere.hent_udlån(borger)

    if udlån is None:
        return False

    for enkelt_udlån in udlån:
        if "grant" not in enkelt_udlån or enkelt_udlån["grant"] is None:
            continue
        
        if str(indsats["basketGrantId"]) == str(
            enkelt_udlån["grant"]["originatorId"]
        ) or str(indsats["currentOrderGrantId"]) == str(
            enkelt_udlån["grant"]["originatorId"]
        ):
            return True

    return False


def afslut_indsats(borger: dict, indsats: dict):
    """
    Afslutter en indsats på en borger.
    param borger: dict - Borgerens data.
    param indsats: dict - Indsatsens data.
    """
    transitioner = {"Bestilt": "Afslut", "Bevilliget": "Annullér", "Ændret": "Afslut"}

    if indsats["workflowState"]["name"] not in transitioner:
        raise WorkItemError(
            f"Kan ikke afslutte indsats på borger {borger['patientIdentifier']['identifier']} med status {indsats['workflowState']['name']}"
        )

    opdateringer_til_indsats = {}

    if transitioner[indsats["workflowState"]["name"]] == "Afslut":
        opdateringer_til_indsats["billingEndDate"] = datetime.now().astimezone().isoformat()
        opdateringer_til_indsats["basketGrantEndDate"] = datetime.now().astimezone().isoformat()
    else:
        opdateringer_til_indsats["cancelledDate"] = datetime.now().astimezone().isoformat()

    nexus.indsats.rediger_indsats(
        indsats, opdateringer_til_indsats, transitioner[indsats["workflowState"]["name"]]
    )


def fjern_organisations_relation_fra_borger(borger: dict):
    """
    Fjerner borgerens relation til organisationen.
    :param borger: Borgerens data.
    """
    udlån = nexus.borgere.hent_udlån(borger)

    if len(udlån) == 0:
        organisationsnavn = "Hjælpemiddelservice"
        organisationer = nexus.organisationer.hent_organisationer_for_borger(borger)

        borger_organisation_relation = next(
            (
                rel
                for rel in organisationer
                if rel["organization"]["name"] == organisationsnavn
            ),
            None,
        )

        if borger_organisation_relation is not None:
            nexus.organisationer.fjern_borger_fra_organisation(
                dict(borger_organisation_relation)
            )
            tracker.track_partial_task(process_name)


if __name__ == "__main__":    
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    credential = Credential.get_credential("KMD Nexus - produktion")
    tracking_credential = Credential.get_credential("Odense SQL Server")
    reporting_credential = Credential.get_credential("RoboA")

    nexus = NexusClientManager(
        client_id=credential.username,
        client_secret=credential.password,
        instance=credential.data["instance"],
    )

    tracker = Tracker(
        username=tracking_credential.username, password=tracking_credential.password
    )

    reporter = Reporter(
        username=reporting_credential.username, password=reporting_credential.password
    )

    logger = logging.getLogger(__name__)

    # Queue management
    if "--queue" in sys.argv:
        workqueue.clear_workqueue("new")
        populate_queue(workqueue)
        exit(0)

    # Process workqueue
    asyncio.run(process_workqueue(workqueue))
