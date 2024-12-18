import os
import json
import boto3
import psycopg2
from datetime import datetime, timezone
import logging

from helpers.vectorstore import update_vectorstore
from langchain_aws import BedrockEmbeddings

# Set up basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

DB_SECRET_NAME = os.environ["SM_DB_CREDENTIALS"]
REGION = os.environ["REGION"]
AILA_DATA_INGESTION_BUCKET = os.environ["BUCKET"]
EMBEDDING_BUCKET_NAME = os.environ["EMBEDDING_BUCKET_NAME"]
RDS_PROXY_ENDPOINT = os.environ["RDS_PROXY_ENDPOINT"]

def get_secret():
    # secretsmanager client to get db credentials
    sm_client = boto3.client("secretsmanager")
    response = sm_client.get_secret_value(SecretId=DB_SECRET_NAME)["SecretString"]
    secret = json.loads(response)
    return secret

def get_parameter(param_name):
    """
    Fetch a parameter value from Systems Manager Parameter Store.
    """
    try:
        ssm_client = boto3.client("ssm")
        response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
        return response["Parameter"]["Value"]
    except Exception as e:
        logger.error(f"Error fetching parameter {param_name}: {e}")
        raise

## GET PARAMETER VALUES FOR CONSTANTS
EMBEDDING_MODEL_ID = get_parameter(os.environ["EMBEDDING_MODEL_PARAM"])

def connect_to_db():
    try:
        db_secret = get_secret()
        connection_params = {
            'dbname': db_secret["dbname"],
            'user': db_secret["username"],
            'password': db_secret["password"],
            'host': RDS_PROXY_ENDPOINT,
            'port': db_secret["port"]
        }
        connection_string = " ".join([f"{key}={value}" for key, value in connection_params.items()])
        connection = psycopg2.connect(connection_string)
        logger.info("Connected to the database!")
        return connection
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        if connection:
            connection.rollback()
            connection.close()
        return None

def parse_s3_file_path(file_key):
    # Assuming the file path is of the format: {course_id}/{module_id}/{documents}/{file_name}.{file_type}
    print(f"file_key: {file_key}")
    try:
        course_id, module_id, file_category, filename_with_ext = file_key.split('/')
        file_name, file_type = filename_with_ext.split('.')
        return course_id, module_id, file_category, file_name, file_type
    except Exception as e:
        logger.error(f"Error parsing S3 file path: {e}")
        return {
                    "statusCode": 400,
                    "body": json.dumps("Error parsing S3 file path.")
                }

# def log_file_deletion(file_key):
#     print(f"File deleted: {file_key}")

def insert_file_into_db(module_id, file_name, file_type, file_path, bucket_name):    
    connection = connect_to_db()
    if connection is None:
        logger.error("No database connection available.")
        return {
            "statusCode": 500,
            "body": json.dumps("Database connection failed.")
        }
    
    try:
        cur = connection.cursor()

        # Check if a record already exists
        select_query = """
        SELECT * FROM "Module_Files"
        WHERE module_id = %s
        AND filename = %s
        AND filetype = %s;
        """
        cur.execute(select_query, (module_id, file_name, file_type))

        existing_file = cur.fetchone()

        if existing_file:
            # Update the existing record
            update_query = """
                UPDATE "Module_Files"
                SET s3_bucket_reference = %s,
                filepath = %s,
                time_uploaded = %s
                WHERE module_id = %s
                AND filename = %s
                AND filetype = %s;
            """
            timestamp = datetime.now(timezone.utc)
            cur.execute(update_query, (
                bucket_name,  # s3_bucket_reference
                file_path,  # filepath
                timestamp,  # time_uploaded
                module_id,  # module_id
                file_name,  # filename
                file_type  # filetype
            ))
            logger.info(f"Successfully updated file {file_name}.{file_type} in database for module {module_id}.")
        else:
            # Insert a new record
            insert_query = """
                INSERT INTO "Module_Files" 
                (module_id, filetype, s3_bucket_reference, filepath, filename, time_uploaded, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """
            timestamp = datetime.now(timezone.utc)
            cur.execute(insert_query, (
                module_id,  # module_id
                file_type,  # filetype
                bucket_name,  # s3_bucket_reference
                file_path,  # filepath
                file_name,  # filename
                timestamp,  # time_uploaded
                ""  # metadata
        ))
        logger.info(f"Successfully inserted file {file_name}.{file_type} into database for module {module_id}.")

        connection.commit()
        cur.close()
        connection.close()
    except Exception as e:
        if cur:
            cur.close()
        if connection:
            connection.rollback()
            connection.close()
        logger.error(f"Error inserting file {file_name}.{file_type} into database: {e}")
        raise

# def delete_course_from_db(course_id):
#     """Deletes the course from the database and cascades the delete."""
#     connection = connect_to_db()
#     if connection is None:
#         logger.error("No database connection available for deleting course.")
#         return False
    
#     try:
#         cur = connection.cursor()
        
#         # Delete from langchain_pg_collection with cascade
#         delete_query = """
#         DELETE FROM langchain_pg_collection
#         WHERE name = %s;
#         """
#         cur.execute(delete_query, (course_id,))
#         connection.commit()
#         logger.info(f"Successfully deleted course {course_id} collection from embeddings.")
        
#         cur.close()
#         connection.close()
#         return True
#     except Exception as e:
#         if cur:
#             cur.close()
#         if connection:
#             connection.rollback()
#             connection.close()
#         logger.error(f"Error deleting course {course_id} collection from embeddings: {e}")
#         return False

def update_vectorstore_from_s3(bucket, course_id):
    
    bedrock_runtime = boto3.client(
        service_name="bedrock-runtime",
        region_name=REGION
    )

    embeddings = BedrockEmbeddings(
        model_id=EMBEDDING_MODEL_ID, 
        client=bedrock_runtime,
        region_name=REGION
    )

    db_secret = get_secret()

    vectorstore_config_dict = {
        'collection_name': f'{course_id}',
        'dbname': db_secret["dbname"],
        'user': db_secret["username"],
        'password': db_secret["password"],
        'host': RDS_PROXY_ENDPOINT,
        'port': db_secret["port"]
    }

    try:
        update_vectorstore(
            bucket=bucket,
            course=course_id,
            vectorstore_config_dict=vectorstore_config_dict,
            embeddings=embeddings
        )
    except Exception as e:
        logger.error(f"Error updating vectorstore for course {course_id}: {e}")
        raise

def handler(event, context):
    records = event.get('Records', [])
    if not records:
        return {
            "statusCode": 400,
            "body": json.dumps("No valid S3 event found.")
        }

    for record in records:
        event_name = record['eventName']
        bucket_name = record['s3']['bucket']['name']

        # Only process files from the AILA_DATA_INGESTION_BUCKET
        if bucket_name != AILA_DATA_INGESTION_BUCKET:
            print(f"Ignoring event from non-target bucket: {bucket_name}")
            continue  # Ignore this event and move to the next one
        file_key = record['s3']['object']['key']

        # if event_name.startswith('ObjectCreated:'):
        # Parse the file path
        course_id, module_id, file_category, file_name, file_type = parse_s3_file_path(file_key)
        if not course_id or not module_id or not file_name or not file_type:
            return {
                "statusCode": 400,
                "body": json.dumps("Error parsing S3 file path.")
            }

        if event_name.startswith('ObjectCreated:'):
            # Insert the file into the PostgreSQL database
            try:
                insert_file_into_db(
                    module_id=module_id,
                    file_name=file_name,
                    file_type=file_type,
                    file_path=file_key,
                    bucket_name=bucket_name
                )
                logger.info(f"File {file_name}.{file_type} inserted successfully.")
            except Exception as e:
                logger.error(f"Error inserting file {file_name}.{file_type} into database: {e}")
                return {
                    "statusCode": 500,
                    "body": json.dumps(f"Error inserting file {file_name}.{file_type}: {e}")
                }
        else:
            logger.info(f"File {file_name}.{file_type} is being deleted. Deleting files from database does not occur here.")
        
        # Update embeddings for course after the file is successfully inserted into the database
        try:
            update_vectorstore_from_s3(bucket_name, course_id)
            logger.info(f"Vectorstore updated successfully for course {course_id}.")
        except Exception as e:
            logger.error(f"Error updating vectorstore for course {course_id}: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(f"File inserted, but error updating vectorstore: {e}")
            }

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "New file inserted into database.",
                "location": f"s3://{bucket_name}/{file_key}"
            })
        }

    return {
        "statusCode": 400,
        "body": json.dumps("No new file upload or deletion event found.")
    }