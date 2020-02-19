from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.hooks.base_hook import BaseHook
from airflow.contrib.operators.slack_webhook_operator import SlackWebhookOperator

import requests
import logging

from helpers.dag_utils import (DagUtility,)
from helpers.mongo_utils import (MongoOperations,)
from helpers.utils import (DataCleaningUtil,)
from helpers.postgres_utils import (PostgresOperations,)
from helpers.slack_utils import (SlackNotification, )
from helpers.configs import (
    ONA_TOKEN, ONA_API_URL, ONA_DBMS, ONA_FORMS, ONA_MONGO_URI,
    ONA_RECREATE_DB, ONA_MONGO_DB_NAME, SLACK_CONN_ID,
)


dag = DAG(
    'pull_data_from_ona',
    default_args=DagUtility.get_dag_default_args()
)


def get_ona_projects():
    """
    load ONA projects from ONA API
    """
    response = requests.get(
        '{}/projects'.format(ONA_API_URL),
        headers={'Authorization': 'Token {}'.format(ONA_TOKEN)}
    )
    return response.json()


def get_ona_form_data(form_id):
    """
    get ONA form data
    :param form_id: form_id
    :return: form data
    """
    if form_id:
        url = "{}data/{}".format(ONA_API_URL, form_id)
        response = requests.get(
            url,
            headers={'Authorization': 'Token {}'.format(ONA_TOKEN)})

        return response.json()

    return []


def clean_form_data_columns(row, table_fields):
    """
    rename columns to conform to db expectations
    :param row: row data coming from ONA API
    :param table_fields: table fields list
    :return: new data object
    """
    new_object = {}
    for item in table_fields:
        new_object[item.db_name] = row.pop(
            item.get('db_name'),
            DataCleaningUtil.set_column_defaults(item.get('type'))
        )

    return new_object


def dump_raw_data_to_mongo(db_connection):
    if ONA_DBMS.lower() == 'mongo' or ONA_DBMS.lower() == 'mongodb':
        ona_projects = get_ona_projects()
        for project in ona_projects:
            for form in project['forms']:
                if form:
                    data = get_ona_form_data(form.get('formid'))
                    collection = db_connection[form.get('name')]
                    collection.insertMany(data)
    else:
        exit(code=1)


def dump_clean_data_to_postgres(primary_key, form, columns, response_data):
    # create the column strings
    column_data = [
        DataCleaningUtil.construct_column_strings(
            item,
            primary_key
        ) for item in form.get('fields')
    ]

    # create the Db
    db_query = PostgresOperations.construct_postgres_create_table_query(
        form.get('name'),
        column_data
    )

    connection = PostgresOperations.establish_postgres_connection()

    with connection:
        cur = connection.cursor()
        if ONA_RECREATE_DB is True:
            cur.execute("DROP TABLE IF EXISTS " + form.get('name'))
            cur.execute(db_query)

        # insert data
        upsert_query = PostgresOperations.construct_postgres_upsert_query(
            form.get('name'),
            columns, primary_key
        )

        cur.executemany(
            upsert_query,
            DataCleaningUtil.update_row_columns(
                form.get('fields'),
                response_data)
        )
        connection.commit()


def dump_clean_data_to_mongo(db_connection, form, data):
    primary_key = form.get('unique_column')
    collection = db_connection[data['project_name']]
    logging.info('Data Size {} end'.format(len(data['data'])))

    formatted_data = [
        clean_form_data_columns(
            item,
            form.get('fields')
        ) for item in data.get('data', None)
    ]

    # construct clean data for saving
    if len(formatted_data) > 0:
        mongo_operations = MongoOperations.construct_mongo_upsert_query(
            formatted_data,
            primary_key
        )

        collection.bulk_write(mongo_operations)


def save_ona_data_to_db(**context):
    """
    save data to MongoDB
    :param context:
    :return:
    """
    db_connection = MongoOperations.establish_mongo_connection(
        ONA_MONGO_URI,
        ONA_MONGO_DB_NAME
    )

    if ONA_FORMS is None or len(ONA_FORMS) == 0:
        # dump raw data to db without formatting the columns
        dump_raw_data_to_mongo(db_connection)

    else:
        all_forms = len(ONA_FORMS)
        success_forms = 0
        for form in ONA_FORMS:

            # get columns
            columns = [item.get('db_name') for item in form.get('fields')]
            primary_key = form.get('unique_column')

            api_data = get_ona_form_data(form.get('form_id'))

            response_data = [
                DataCleaningUtil.clean_key_field(
                    item,
                    primary_key
                ) for item in api_data
            ]

            if isinstance(response_data, (list,)) and len(response_data):
                if ONA_DBMS.lower() == 'postgres' or ONA_DBMS.lower() == 'postgresdb':
                    """
                    Dump data to postgres 
                    """
                    dump_clean_data_to_postgres(primary_key, form, columns, response_data)
                    success_forms += 1
                else:
                    """
                    Dump Data to MongoDB
                    """
                    dump_clean_data_to_mongo(db_connection, form, response_data)
                    success_forms += 1
            else:
                print(dict(message='The form {} has no data'.format(form.get('name'))))

        if success_forms == all_forms:
            return dict(success=True)
        else:
            return dict(failure='Not all forms data loaded or other forms had no data')


def sync_submissions_on_db(**context):
    """
    delete submissions that nolonger exist on API
    :param context:
    :return:
    """
    pass


def task_success_slack_notification(context):
    slack_webhook_token = BaseHook.get_connection(SLACK_CONN_ID).password
    attachments = SlackNotification.construct_slack_message(context, 'success')

    failed_alert = SlackWebhookOperator(
        task_id='slack_test',
        http_conn_id='slack',
        webhook_token=slack_webhook_token,
        attachments=attachments,
        username='airflow'
    )
    return failed_alert.execute(context=context)


def task_failed_slack_notification(context):
    slack_webhook_token = BaseHook.get_connection(SLACK_CONN_ID).password
    attachments = SlackNotification.construct_slack_message(context, 'failed')

    failed_alert = SlackWebhookOperator(
        task_id='slack_test',
        http_conn_id='slack',
        webhook_token=slack_webhook_token,
        attachments=attachments,
        username='airflow')
    return failed_alert.execute(context=context)


# TASKS
save_ONA_data_to_db_task = PythonOperator(
    task_id='Save_ONA_data_to_db',
    provide_context=True,
    python_callable=save_ona_data_to_db,
    on_failure_callback=task_failed_slack_notification,
    on_success_callback=task_success_slack_notification,
    dag=dag,
)


sync_ONA_submissions_on_db_task = PythonOperator(
    task_id='Sync_ONA_data_with_db',
    provide_context=True,
    python_callable=sync_submissions_on_db,
    on_failure_callback=task_failed_slack_notification,
    on_success_callback=task_success_slack_notification,
    dag=dag,
)

save_ONA_data_to_db_task >> sync_ONA_submissions_on_db_task
