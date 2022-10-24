#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#


import datetime
import json
import os
import random
import threading
import time
from typing import Dict, Generator

from airbyte_cdk.logger import AirbyteLogger
from airbyte_cdk.models import (
    AirbyteCatalog,
    AirbyteConnectionStatus,
    AirbyteLogMessage,
    AirbyteMessage,
    AirbyteRecordMessage,
    AirbyteStateMessage,
    AirbyteStream,
    ConfiguredAirbyteCatalog,
    Status,
    Type,
)
from airbyte_cdk.sources import Source
from mimesis import Datetime, Person
from mimesis.locales import Locale

THREADS_ACTIVE = False


class SourceFaker(Source):
    def check(self, logger: AirbyteLogger, config: Dict[str, any]) -> AirbyteConnectionStatus:
        """
        Tests if the input configuration can be used to successfully connect to the integration
            e.g: if a provided Stripe API token can be used to connect to the Stripe API.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this source, content of this json is as specified in
        the properties of the spec.json file

        :return: AirbyteConnectionStatus indicating a Success or Failure
        """

        # As this is an in-memory source, it always succeeds
        return AirbyteConnectionStatus(status=Status.SUCCEEDED)

    def discover(self, logger: AirbyteLogger, config: Dict[str, any]) -> AirbyteCatalog:
        """
        Returns an AirbyteCatalog representing the available streams and fields in this integration.
        For example, given valid credentials to a Postgres database,
        returns an Airbyte catalog where each postgres table is a stream, and each table column is a field.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this source, content of this json is as specified in
        the properties of the spec.json file

        :return: AirbyteCatalog is an object describing a list of all available streams in this source.
            A stream is an AirbyteStream object that includes:
            - its stream name (or table name in the case of Postgres)
            - json_schema providing the specifications of expected schema for this stream (a list of columns described
            by their names and types)
        """
        streams = []
        dirname = os.path.dirname(os.path.realpath(__file__))

        # Fake Users
        spec_path = os.path.join(dirname, "users_catalog.json")
        catalog = read_json(spec_path)
        streams.append(AirbyteStream(name="Users", json_schema=catalog, supported_sync_modes=["full_refresh", "incremental"]))

        # Fake Products
        spec_path = os.path.join(dirname, "products_catalog.json")
        catalog = read_json(spec_path)
        streams.append(AirbyteStream(name="Products", json_schema=catalog, supported_sync_modes=["full_refresh"]))

        # Fake Purchases
        spec_path = os.path.join(dirname, "purchases_catalog.json")
        catalog = read_json(spec_path)
        streams.append(AirbyteStream(name="Purchases", json_schema=catalog, supported_sync_modes=["full_refresh", "incremental"]))

        return AirbyteCatalog(streams=streams)

    def read(
        self, logger: AirbyteLogger, config: Dict[str, any], catalog: ConfiguredAirbyteCatalog, state: Dict[str, any]
    ) -> Generator[AirbyteMessage, None, None]:
        """
        Returns a generator of the AirbyteMessages generated by reading the source with the given configuration,
        catalog, and state.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this source, content of this json is as specified in
            the properties of the spec.json file
        :param catalog: The input catalog is a ConfiguredAirbyteCatalog which is almost the same as AirbyteCatalog
            returned by discover(), but
        in addition, it's been configured in the UI! For each particular stream and field, there may have been provided
        with extra modifications such as: filtering streams and/or columns out, renaming some entities, etc
        :param state: When a Airbyte reads data from a source, it might need to keep a checkpoint cursor to resume
            replication in the future from that saved checkpoint.
            This is the object that is provided with state from previous runs and avoid replicating the entire set of
            data everytime.

        :return: A generator that produces a stream of AirbyteRecordMessage contained in AirbyteMessage object.
        """

        count: int = config["count"] if "count" in config else 0
        seed: int = config["seed"] if "seed" in config else None
        records_per_sync: int = config["records_per_sync"] if "records_per_sync" in config else 500
        records_per_slice: int = config["records_per_slice"] if "records_per_slice" in config else 100

        to_generate_users = False
        to_generate_purchases = False
        purchases_stream = None
        purchases_count = state["Purchases"]["purchases_count"] if "Purchases" in state else 0
        for stream in catalog.streams:
            if stream.stream.name == "Users":
                to_generate_users = True
        for stream in catalog.streams:
            if stream.stream.name == "Purchases":
                purchases_stream = stream
                to_generate_purchases = True

        if to_generate_purchases and not to_generate_users:
            raise ValueError("Purchases stream cannot be enabled without Users stream")

        for stream in catalog.streams:
            yield log_stream(stream.stream.name)

            if stream.stream.name == "Users":
                cursor = get_stream_cursor(state, stream.stream.name)
                total_records = cursor
                records_in_sync = 0
                records_in_page = 0

                users_buffer = []
                users_thread_pool: list[threading.Thread] = []

                start_user_threads(users_buffer, users_thread_pool, seed)

                for i in range(cursor, count):
                    user = wait_pop(users_buffer)
                    user["id"] = i + 1
                    yield generate_record(stream, user)
                    total_records += 1
                    records_in_sync += 1
                    records_in_page += 1

                    if to_generate_purchases:
                        purchases = generate_purchases(user, purchases_count)
                        for p in purchases:
                            yield generate_record(purchases_stream, p)
                            purchases_count += 1

                    if records_in_page == records_per_slice:
                        yield generate_state(state, stream, {"cursor": total_records, "seed": seed})
                        records_in_page = 0

                    if records_in_sync == records_per_sync:
                        break

                yield generate_state(state, stream, {"cursor": total_records, "seed": seed})
                if purchases_stream is not None:
                    yield generate_state(state, purchases_stream, {"purchases_count": purchases_count})

                stop_user_threads(users_thread_pool)

            elif stream.stream.name == "Products":
                products = generate_products()
                for p in products:
                    yield generate_record(stream, p)
                yield generate_state(state, stream, {"product_count": len(products)})

            elif stream.stream.name == "Purchases":
                # Purchases are generated as part of Users stream
                True

            else:
                raise ValueError(stream.stream.name)


def wait_pop(buffer: list, max_tries=300, sleep=0.01):
    # default is 3s of trying
    result = None
    attempt = 0
    while result is None and attempt < max_tries:
        attempt = attempt + 1
        try:
            result = buffer.pop()
        except IndexError:
            time.sleep(sleep)

    if result is None:
        raise Exception(f"Could not get an element from pool in {sleep * max_tries}s")

    return result


def get_stream_cursor(state: Dict[str, any], stream: str) -> int:
    cursor = (state[stream]["cursor"] or 0) if stream in state else 0
    return cursor


def generate_record(stream: any, data: any):
    dict = data.copy()

    # timestamps need to be emitted in ISO format
    for key in dict:
        if isinstance(dict[key], datetime.datetime):
            dict[key] = format_airbyte_time(dict[key])

    return AirbyteMessage(
        type=Type.RECORD,
        record=AirbyteRecordMessage(stream=stream.stream.name, data=dict, emitted_at=int(datetime.datetime.now().timestamp()) * 1000),
    )


def log_stream(stream_name: str):
    return AirbyteMessage(
        type=Type.LOG,
        log=AirbyteLogMessage(
            message="Sending data for stream: " + stream_name,
            level="INFO",
            emitted_at=int(datetime.datetime.now().timestamp()) * 1000,
        ),
    )


def generate_state(state: Dict[str, any], stream: any, data: any):
    state[
        stream.stream.name
    ] = data  # since we have multiple streams, we need to build up the "combined state" for all streams and emit that each time until the platform has support for per-stream state
    return AirbyteMessage(type=Type.STATE, state=AirbyteStateMessage(data=state))


def generate_user(person: Person, dt: Datetime):
    time_a = dt.datetime()
    time_b = dt.datetime()

    profile = {
        "created_at": time_a if time_a <= time_b else time_b,
        "updated_at": time_a if time_a > time_b else time_b,
        "name": person.name(),
        "title": person.title(),
        "age": person.age(),
        "email": person.email(),
        "telephone": person.telephone(),
        "gender": person.gender(),
        "language": person.language(),
        "academic_degree": person.academic_degree(),
        "nationality": person.nationality(),
        "occupation": person.occupation(),
        "height": person.height(),
        "blood_type": person.blood_type(),
        "weight": person.weight(),
    }

    while not profile["created_at"]:
        profile["created_at"] = dt.datetime()

    if not profile["updated_at"]:
        profile["updated_at"] = profile["created_at"] + 1

    return profile


def generate_purchases(user: any, purchases_count: int) -> list[Dict]:
    purchases: list[Dict] = []
    purchase_percent_remaining = 80  # ~ 20% of people will have no purchases
    total_products = len(generate_products())
    purchase_percent_remaining = purchase_percent_remaining - random.randrange(1, 100)
    i = 0
    while purchase_percent_remaining > 0:
        id = purchases_count + i + 1
        product_id = random.randrange(1, total_products)
        added_to_cart_at = random_date_in_range(user["created_at"])
        purchased_at = (
            random_date_in_range(added_to_cart_at) if added_to_cart_at is not None and random.randrange(1, 100) <= 70 else None
        )  # 70% likely to purchase the item in the cart
        returned_at = (
            random_date_in_range(purchased_at) if purchased_at is not None and random.randrange(1, 100) <= 15 else None
        )  # 15% likely to return the item
        purchase = {
            "id": id,
            "product_id": product_id,
            "user_id": user["id"],
            "added_to_cart_at": added_to_cart_at,
            "purchased_at": purchased_at,
            "returned_at": returned_at,
        }
        purchases.append(purchase)

        purchase_percent_remaining = purchase_percent_remaining - random.randrange(1, 100)
        i += 1
    return purchases


def generate_products() -> list[Dict]:
    dirname = os.path.dirname(os.path.realpath(__file__))
    return read_json(os.path.join(dirname, "products.json"))


def start_user_threads(object_pool: list, threads: list[threading.Thread], seed, parallelism=10):
    global THREADS_ACTIVE

    THREADS_ACTIVE = True
    i = 0
    while i < parallelism:
        name = f"thread-{i + 1}"
        print(f"Starting thread {name}")
        t = threading.Thread(target=run_user_thread, name=name, args=(object_pool, seed))
        t.start()
        threads.append(t)
        i = i + 1


def stop_user_threads(threads: list[threading.Thread]):
    global THREADS_ACTIVE

    THREADS_ACTIVE = False

    while len(threads) > 0:
        t = threads.pop()
        t.join()


def run_user_thread(object_pool: list, seed: int, buffer_size=1000):
    global THREADS_ACTIVE
    name = threading.current_thread().name
    id = int(name.split("-")[1])

    if THREADS_ACTIVE is False:
        print(f"Stopping thread {name}")
        return

    person = Person(locale=Locale.EN, seed=seed)
    dt = Datetime(seed=seed)

    while len(object_pool) < buffer_size:
        user = generate_user(person, dt)
        object_pool.append(user)

    time.sleep(0.1 * id)

    run_user_thread(object_pool, seed, buffer_size)


def read_json(filepath):
    with open(filepath, "r") as f:
        return json.loads(f.read())


def random_date_in_range(start_date: datetime.datetime, end_date: datetime.datetime = datetime.datetime.now()) -> datetime.datetime:
    time_between_dates = end_date - start_date
    days_between_dates = time_between_dates.days
    if days_between_dates < 2:
        days_between_dates = 2
    random_number_of_days = random.randrange(days_between_dates)
    random_date = start_date + datetime.timedelta(days=random_number_of_days)
    return random_date


def format_airbyte_time(d: datetime):
    s = f"{d}"
    s = s.split(".")[0]
    s = s.replace(" ", "T")
    s += "+00:00"
    return s
