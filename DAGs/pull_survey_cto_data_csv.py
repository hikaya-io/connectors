import logging
import requests
import re
from datetime import timedelta

from airflow import DAG, AirflowException
from airflow.operators.python_operator import PythonOperator
from helpers.utils import DataCleaningUtil, logger
from helpers.mongo_utils import MongoOperations
from helpers.postgres_utils import PostgresOperations
from helpers.requests import create_requests_session
from helpers.task_utils import notify, get_daily_start_date
from helpers.configs import (
    SURV_SERVER_NAME,
    SURV_USERNAME,
    SURV_PASSWORD,
    SURV_DBMS,
    SURV_MONGO_DB_NAME,
    SURV_MONGO_URI,
    POSTGRES_DB,
)

logger = logging.getLogger(__name__)

DAG_NAME = "dots_survey_cto_data_csv_pipeline"
PIPELINE = "surveycto"

default_args = {
    "owner": "Hikaya-Dots",
    "depends_on_past": False,
    "start_date": get_daily_start_date(),
    "catchup": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "on_failure_callback": notify(status="failed", pipeline=PIPELINE),
    "on_success_callback": notify(status="success", pipeline=PIPELINE),
}


def get_forms():
    print("a")


def import_form_submissions(form):
    """
    CSV SCTO import documentation: https://docs.surveycto.com/05-exporting-and-publishing-data/01-overview/09.data-format.html
    """
    form_id = form.get("id")
    auth_basic = requests.auth.HTTPBasicAuth(SURV_USERNAME, SURV_PASSWORD)
    # CSV direct
    csv_url = (
        f"https://{SURV_SERVER_NAME}.surveycto.com/api/v1/forms/data/csv/{form_id}"
    )
    test = requests.get(csv_url, auth=auth_basic)

    # Pandas loading
    from io import StringIO
    import pandas

    string_io = StringIO(test.text)
    df = pandas.read_csv(string_io)
    # print(df.shape)
    # print(df.head(5))
    print(df.info())
    print(df.describe())

    session = create_requests_session(debug=False)
    try:
        res = session.get(f"https://{SURV_SERVER_NAME}.surveycto.com/")
        res.raise_for_status()
        csrf_token = re.search(r"var csrfToken = '(.+?)';", res.text).group(1)
        print("csrf_token")
        print(csrf_token)
    except requests.exceptions.HTTPError as e:
        logger.error(e)
        raise AirflowException(
            "Couldn't load SurveyCTO landing page for getting the CSRF token"
        )
    except requests.exceptions.RequestException as e:
        logger.error(e)
        raise AirflowException("Unexpected error loading SurveyCTO landing page")

    form_details = session.get(
        f"https://{SURV_SERVER_NAME}.surveycto.com/forms/{form_id}/workbook/export/load",
        params={
            "includeFormStructureModel": "true",
            "submissionsPattern": "all",
            "fieldsPattern": "all",
            "fetchInBatches": "true",
            "includeDatasets": "false",
            "date": "1550011019966",  # TODO set date conveniently
        },
        auth=auth_basic,
        headers={
            "X-csrf-token": csrf_token,
            "X-OpenRosa-Version": "1.0",
            "Accept": "*/*",
        },
    )
    if form_details.status_code == 200:
        form_details = form_details.json()
        print(form_details)
    else:
        print(form_details)
        print(form_details.text)

    form_structure_model = form_details.get("formStructureModel")
    first_language = form_structure_model.get("defaultLanguage")
    fields = form_structure_model["summaryElementsPerLanguage"][first_language][
        "children"
    ]
    print(fields)

    repeat_fields = get_repeat_groups(fields)
    print("repeat_fields")
    print(repeat_fields)

    for field in repeat_fields:
        if field.get("dataType") == "repeat":
            print("Found a repeat group...===================")
            print(field)
            csv_url = f"https://{SURV_SERVER_NAME}.surveycto.com/api/v1/forms/data/csv/{form_id}/{field.get('name')}"
            test = requests.get(csv_url, auth=auth_basic)
            string_io = StringIO(test.text)
            df = pandas.read_csv(string_io)
            print(df.shape)
            print(df.head(5))
            print(df.describe())
            # Group them by "PARENT KEY"
            grouped = df.groupby("PARENT_KEY")
            print("GROUPED")
            print(grouped)
            # print(grouped.shape)
            print(grouped.head(5))
            print(grouped.describe())


def get_repeat_groups(fields):
    """
    Return an array of repeat groups and their details from a list of nested fields
    """
    repeat_fields = []
    for field in fields:
        if field.get("dataType") == "repeat":
            repeat_fields.append(field)
        elif field.get("children"):
            # repeat_fields.append(get_repeat_groups(field.get("children")))
            repeat_fields = repeat_fields + get_repeat_groups(field.get("children"))
    return repeat_fields


def import_forms_and_submissions(**kwargs):
    # import_form_submissions({"id": "airflow_sample_form"})
    import_form_submissions({"id": "baseline_bmz_v1"})


with DAG(DAG_NAME, default_args=default_args, schedule_interval="@daily") as dag:

    dag.doc_md = """
    Doc
    """
    t1 = PythonOperator(
        task_id="Save_data_to_DB",
        python_callable=import_forms_and_submissions,
        dag=dag,
    )
    t1
