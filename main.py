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
    NexusClient,
    CitizensClient,
    OrganizationsClient,
    GrantsClient,
    filter_references,
)
from odk_tools.tracking import Tracker
from odk_tools.reporting import Reporter

nexus_client: NexusClient = None
citizens_client: CitizensClient = None
organizations_client: OrganizationsClient = None
grants_client: GrantsClient = None
tracker: Tracker = None
reporter: Reporter = None
logger = None
process_name = "Oprydning af hjælpemiddelsindsatser"


def populate_queue(workqueue: Workqueue):
    organization = organizations_client.get_organization_by_name("Hjælpemiddelservice")
    citizens = organizations_client.get_citizens_by_organization(organization)

    for citizen in citizens:
        try:
            cpr = citizen["patientIdentifier"]["identifier"]
            workqueue.add_item({}, cpr)
        except Exception as e:
            logger.error(f"Error adding citizen to Queue {citizen}: {e}")


async def process_workqueue(workqueue: Workqueue):
    for item in workqueue:
        with item:

            try:
                citizen = citizens_client.get_citizen(item.reference)

                if citizen is None:                    
                    continue

                # Kontrollerer om borgeren har udlån uden indsats
                if check_lendings_without_basket_grants(citizen):
                    reporter.report(
                        process_name,
                        "Borgere med udlån uden indsats",
                        {"Cpr": citizen["patientIdentifier"]["identifier"]},
                    )

                inactivate_basket_grants(citizen)
                remove_relation(citizen)
            except ValueError as e:
                logger.error(f"Error getting citizen data: {e}")
                item.fail("Ugyldigt cpr-nummer")
            except WorkItemError as e:
                item.fail(str(e))


def check_lendings_without_basket_grants(citizen_data: dict) -> bool:
    """
    Check if there are any lendings without a basket grant.
    param citizen_data: dict - The citizen data.
    return: bool - True if there are lendings without a basket grant, False otherwise.
    """
    lending_without_basket_grant = False
    lendings = citizens_client.get_citizen_lendings(citizen_data)

    if lendings is None:
        return lending_without_basket_grant

    for lending in lendings:
        if (
            "grant" not in lending
            or lending["grant"]["originatorStatus"] == "Afsluttet"
            or lending["grant"] is None
        ):
            logging.info(f"Lending found without basket grant: {lending}")
            lending_without_basket_grant = True

    return lending_without_basket_grant


def inactivate_basket_grants(citizen: dict):
    """
    Inactivate basket grants for a citizen if certain conditions are met.
    param citizen: dict - The citizen data.
    """
    pathway = citizens_client.get_citizen_pathway(citizen)
    basket_grant_references = citizens_client.get_citizen_pathway_references(pathway)
    filtered_references = filter_references(
        basket_grant_references,
        path="/Sundhedsfagligt grundforløb/*/Indsatser/basketGrantReference",
        active_pathways_only=False,
    )

    for reference in filtered_references:
        basket_grant = citizens_client.resolve_reference(reference)

        if basket_grant["workflowState"]["name"] in [
            "Afsluttet",
            "Annulleret",
            "Afslået",
        ]:
            continue

        elements = grants_client.get_grant_elements(basket_grant)

        # Store variationer i elements gør det svært at checke korrekt
        try:
            name = elements["supplier"]["supplier"]["name"]
            if name != "Hjælpemiddelservice":
                continue
        except Exception:
            continue

        if basket_grant["workflowState"]["name"] == "Bevilliget":
            # Hvis udlånet er indenfor de sidste 31 dage, så skal det ikke afsluttes. Bemærk dato fr nexus har timezone info.
            if elements["workflowRequestedDate"] > datetime.now().astimezone() - timedelta(days=31):
                logging.warning(
                    f"Potentielt aktivt udlån på borger {citizen['patientIdentifier']['identifier']}"
                )
                continue

        if active_lendings(citizen, basket_grant):
            continue

        basket_grant_end_date = elements.get("basketGrantEndDate")

        if basket_grant_end_date:
            if basket_grant_end_date > datetime.now().astimezone():
                continue

        logger.info(
            f"Trying to edit basket grant with id: {basket_grant['basketGrantId']} for citizen: {citizen['patientIdentifier']['identifier']}"
        )

        inactivate_basket_grant(citizen, basket_grant)
        tracker.track_task(process_name)


def active_lendings(citizen: dict, basket_grant: dict) -> bool:
    """
    Check if there are any active lendings that match the basket grant.
    param citizen_data: dict - The citizen.
    param basket_grant: dict - The basket grant.
    return: bool - True if there are active lendings that match the basket grant, False otherwise.
    """
    lendings = citizens_client.get_citizen_lendings(citizen)

    if lendings is None:
        return False

    for lending in lendings:
        if str(basket_grant["basketGrantId"]) == str(
            lending["grant"]["originatorId"]
        ) or str(basket_grant["currentOrderGrantId"]) == str(
            lending["grant"]["originatorId"]
        ):
            return True

    return False


def inactivate_basket_grant(citizen: dict, basket_grant: dict):
    """
    Inactivate a basket grant for a citizen.
    param citizen: dict - The citizen data.
    param basket_grant: dict - The basket grant data.
    """
    transitions = {"Bestilt": "Afslut", "Bevilliget": "Annullér", "Ændret": "Afslut"}

    if basket_grant["workflowState"]["name"] not in transitions:
        raise WorkItemError(
            f"Kan ikke afslutte indsats på borger {citizen['patientIdentifier']['identifier']} med status {basket_grant['workflowState']['name']}"
        )

    field_updates = {}

    if transitions[basket_grant["workflowState"]["name"]] == "Afslut":
        field_updates["billingEndDate"] = datetime.now().astimezone().isoformat()
        field_updates["basketGrantEndDate"] = datetime.now().astimezone().isoformat()
    else:
        field_updates["cancelledDate"] = datetime.now().astimezone().isoformat()

    grants_client.edit_grant(
        basket_grant, field_updates, transitions[basket_grant["workflowState"]["name"]]
    )


def remove_relation(citizen_data: dict):
    """
    Remove the relation between the citizen and the organization if there are no active lendings.
    param citizen_data: dict - The citizen data.
    """
    lendings = citizens_client.get_citizen_lendings(citizen_data)

    if len(lendings) == 0:
        organization_name = "Hjælpemiddelservice"
        organizations = organizations_client.get_organizations_by_citizen(citizen_data)

        filtered_organization = next(
            (
                rel
                for rel in organizations
                if rel["organization"]["name"] == organization_name
            ),
            None,
        )

        if filtered_organization is not None:
            organizations_client.remove_citizen_from_organization(
                dict(filtered_organization)
            )
            tracker.track_partial_task(process_name)


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    credential = Credential.get_credential("KMD Nexus - produktion")
    tracking_credential = Credential.get_credential("Odense SQL Server")
    reporting_credential = Credential.get_credential("RoboA")

    nexus_client = NexusClient(
        client_id=credential.username,
        client_secret=credential.password,
        instance=credential.data["instance"],
    )
    citizens_client = CitizensClient(nexus_client=nexus_client)
    organizations_client = OrganizationsClient(nexus_client=nexus_client)
    grants_client = GrantsClient(nexus_client=nexus_client)

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
