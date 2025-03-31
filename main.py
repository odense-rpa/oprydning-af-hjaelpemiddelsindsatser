import asyncio
import logging
import sys

from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from automation_server_client import AutomationServer, Workqueue, WorkItemError, Credential
from kmd_nexus_client import NexusClient, CitizensClient, OrganizationsClient, GrantsClient, filter_references
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
            cpr =  citizen["patientIdentifier"]["identifier"]            
            workqueue.add_item({"Cpr": cpr}, cpr)
        except Exception as e:
            logging.error(f"Error adding citizen to Queue {citizen}: {e}")


async def process_workqueue(workqueue: Workqueue):
    for item in workqueue:
        with item:
            data = item.get_data_as_dict()
            data = citizens_client.get_citizen(data.get("Cpr"))
            
            try:
                report_lendings_without_basket_grants = check_lendings_without_basket_grants(data)
                manage_related_basket_grants(data)
                remove_relation(data)

                if report_lendings_without_basket_grants:
                    reporter.report(process_name, "Borgere med udlån uden indsats", {'Cpr': data["patientIdentifier"]["identifier"]})

                tracker.track_task(process_name)        

            except WorkItemError as e:
                logger.warning(f"Error processing work item: {e}")


def check_lendings_without_basket_grants(citizen_data: dict) -> bool:
    lending_without_basket_grant = False
    lendings = citizens_client.get_citizen_lendings(citizen_data)
        
    for lending in lendings:
        if "grant" not in lending or lending["grant"]["originatorStatus"] == "Afsluttet" or lending["grant"] is None:
            logging.info(f"Lending found without basket grant: {lending}")
            lending_without_basket_grant = True

    return lending_without_basket_grant


def manage_related_basket_grants(citizen_data: dict):
    pathway = citizens_client.get_citizen_pathway(citizen_data)
    basket_grant_references = citizens_client.get_citizen_pathway_references(pathway)    
    filtered_references = filter_references(
        basket_grant_references,
        path = "/Sundhedsfagligt grundforløb/*/Indsatser/basketGrantReference",
        active_pathways_only = False
    )
    
    for reference in filtered_references:
        basket_grant = citizens_client.resolve_reference(reference)

        if basket_grant["workflowState"]["name"] in ["Afsluttet", "Annulleret", "Afslået"]:
            continue

        elements = grants_client.get_grant_elements(basket_grant)
        
        if "supplier" not in elements:
            continue

        if (isinstance(elements.get("supplier"), dict) and isinstance(elements["supplier"].get("supplier"), dict) and elements["supplier"]["supplier"].get("name") == "Hjælpemiddelservice"):
            active_lending_matching_basket_grant = get_active_lending_matching_basket_grant(citizen_data, basket_grant)
            
            if not active_lending_matching_basket_grant:
                logger.info(f"Trying to edit basket grant with id: {basket_grant['basketGrantId']} for citizen: {citizen_data['patientIdentifier']['identifier']}")
                inactivate_basket_grant(citizen_data, basket_grant, elements)


def get_active_lending_matching_basket_grant(citizen_data: dict, basket_grant: dict) -> bool:
    lendings = citizens_client.get_citizen_lendings(citizen_data)

    for lending in lendings:
        if str(basket_grant["basketGrantId"]) == str(lending["grant"]["originatorId"]) or str(basket_grant["currentOrderGrantId"]) == str(lending["grant"]["originatorId"]):
            return True
        
    return False


def inactivate_basket_grant(citizen_data: dict, basket_grant: dict, elements: dict):
    transitions = {
        "Bestilt": "Afslut",
        "Bevilliget": "Annullér",
        "Ændret": "Afslut"
    }

    if basket_grant["workflowState"]["name"] not in transitions:
        logging.error(f"Kan ikke afslutte indsats på borger {citizen_data['patientIdentifier']['identifier']} med status {basket_grant['workflowState']['name']}")
        return

    if basket_grant["workflowState"]["name"] == "Bevilliget":
        requested_date = elements["workflowRequestedDate"]
        now = datetime.now(timezone.utc)
        one_month_ago = now - relativedelta(months=1)
        
        if requested_date > one_month_ago:
            logging.warning(f"Potentielt aktivt udlån på borger {citizen_data['patientIdentifier']['identifier']}")
            return        

    field_updates = {}    
    
    if(transitions[basket_grant["workflowState"]["name"]] == "Afslut"):
        field_updates = {
            "billingEndDate": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000',
            "basketGrantEndDate": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'
        }
    else:
        field_updates = {
            "cancelledDate": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0000'            
        }
    
    grants_client.edit_grant(basket_grant, field_updates, transitions[basket_grant["workflowState"]["name"]])    
    

def remove_relation(citizen_data: dict):    
    lendings = citizens_client.get_citizen_lendings(citizen_data)
    
    if len(lendings) == 0:
        organization_name = "Hjælpemiddelservice"
        organizations = organizations_client.get_organizations_by_citizen(citizen_data)
        
        filtered_organization = next(
            (rel for rel in organizations if rel["organization"]["name"] == organization_name),
            None
        )    
    
        if filtered_organization is not None:
            organizations_client.remove_citizen_from_organization(dict(filtered_organization))


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()    
        
    credential = Credential.get_credential("KMD Nexus - produktion")
    tracking_credential = Credential.get_credential("Odense SQL Server")
    reporting_credential = Credential.get_credential("RoboA")

    nexus_client = NexusClient(client_id=credential.username, client_secret=credential.password, instance=credential.get_data_as_dict()["instance"])
    citizens_client = CitizensClient(nexus_client=nexus_client)
    organizations_client = OrganizationsClient(nexus_client=nexus_client)
    grants_client = GrantsClient(nexus_client=nexus_client)

    tracker = Tracker(
        username = tracking_credential.username, 
        password = tracking_credential.password
    )

    reporter = Reporter(
        username = reporting_credential.username, 
        password = reporting_credential.password
    )

    logger = logging.getLogger(__name__)        

    # Queue management
    if "--queue" in sys.argv:
        workqueue.clear_workqueue("new")
        populate_queue(workqueue)
        exit(0)

    # Process workqueue
    asyncio.run(process_workqueue(workqueue))
