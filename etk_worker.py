import time
from datetime import datetime
import json
import sys
import os
from argparse import ArgumentParser
import signal
import logging

from kafka import KafkaProducer, KafkaConsumer
# from digsandpaper.elasticsearch_indexing.index_knowledge_graph import index_knowledge_graph_fields

from config import config
sys.path.append(os.path.join(config['etk_path']))
sys.path.append(os.path.join(config['etk_path'], 'etk'))
from etk.etk import ETK
from etk.knowledge_graph import KGSchema

g_etk_worker = None
g_logger = None

class ETKWorker(object):

    def __init__(self, master_config, em_paths, logger, worker_id,
                 project_name, kafka_input_args=None, kafka_output_args=None):
        self.logger = logger
        self.worker_id = worker_id
        self.check_interval = 1000
        self.exit_sign = False

        try:
            kg_schema = KGSchema(master_config)
            self.etk_ins = ETK(kg_schema, em_paths, logger=logger)
        except Exception as e:
            logger.exception('ETK initialization failed')
            raise e

        # kafka input
        self.kafka_input_server = config['input_server']
        self.kafka_input_session_timeout = config['input_session_timeout']
        self.kafka_input_group_id = config['input_group_id']
        self.kafka_input_topic = '{project_name}_in'.format(project_name=project_name)
        self.kafka_input_args = dict() if kafka_input_args is None else kafka_input_args
        self.kafka_consumer = KafkaConsumer(
            bootstrap_servers=self.kafka_input_server,
            group_id=self.kafka_input_group_id,
            consumer_timeout_ms=self.check_interval,
            value_deserializer=lambda v: json.loads(v.decode('utf-8')),
            **self.kafka_input_args
        )
        self.kafka_consumer.subscribe([self.kafka_input_topic])

        # kafka output
        self.kafka_output_server = config['output_server']
        self.kafka_output_topic = '{project_name}_out'.format(project_name=project_name)
        self.kafka_output_args = dict() if kafka_output_args is None else kafka_output_args
        self.kafka_producer = KafkaProducer(
            bootstrap_servers=self.kafka_output_server,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            **self.kafka_output_args
        )

        self.timeout_count = self.kafka_input_session_timeout / self.check_interval

    def process(self):
        prev_doc_sent_time = None

        while not self.exit_sign:
            # high level api handles batching
            # will exit once timeout
            try:
                for msg in self.kafka_consumer:
                    # force to commit, block till getting response
                    self.kafka_consumer.commit()

                    cdr = msg.value
                    cdr['@execution_profile'] = {'@worker_id': self.worker_id}
                    doc_arrived_time = time.time()
                    cdr['@execution_profile']['@doc_arrived_time'] = \
                        datetime.utcfromtimestamp(doc_arrived_time).isoformat()
                    cdr['@execution_profile']['@doc_wait_time'] = \
                        0.0 if not prev_doc_sent_time \
                        else float(doc_arrived_time - prev_doc_sent_time)
                    cdr['@execution_profile']['@doc_length'] = len(json.dumps(cdr))

                    if 'doc_id' not in cdr or len(cdr['doc_id']) == 0:
                        self.logger.error('invalid cdr: unknown doc_id')
                        continue

                    self.logger.info('processing', cdr['doc_id'])
                    try:
                        start_run_core_time = time.time()
                        # run etk module

                        doc = self.etk_ins.create_document(cdr)
                        doc, kg = self.etk_ins.process_ems(doc)
                        cdr = kg

                        # indexing
                        # TODO
                        # indexed_kg = index_knowledge_graph_fields(kg)
                        # if not indexed_kg:
                        #     logger.error('indexing in sandpaper failed')
                        #     continue
                        # cdr = indexed_kg

                        cdr['@execution_profile']['@run_core_time'] = \
                            float(time.time() - start_run_core_time)
                        doc_sent_time = time.time()
                        cdr['@execution_profile']['@doc_sent_time'] = \
                            datetime.utcfromtimestamp(doc_sent_time).isoformat()
                        prev_doc_sent_time = doc_sent_time
                        cdr['@execution_profile']['@doc_processed_time'] =\
                            float(doc_sent_time - doc_arrived_time)

                        # output result
                        r = self.kafka_producer.send(self.kafka_output_topic, cdr)
                        r.get(timeout=60)  # wait till sent

                        self.logger.info('{} done'.format(cdr['doc_id']))

                    except Exception as e:
                        self.logger.exception('failed at', cdr['doc_id'])

            except ValueError as e:
                # I/O operation on closed epoll fd
                self.logger.info('consumer closed')
                sys.exit()

            except StopIteration as e:
                # timeout
                self.timeout_count -= 1
                if self.timeout_count <= 0:
                    self.exit_sign = True

    def __del__(self):

        self.logger.info('ETK worker {} is exiting...'.format(self.worker_id))

        try:
            self.kafka_consumer.close()
        except:
            pass
        try:
            self.kafka_producer.close()
        except:
            pass

def termination_handler(signum, frame):
    global g_logger, g_etk_worker
    g_logger.info('SIGNAL #{} received, trying to exit...'.format(signum))

    try:
        g_etk_worker.exit_sign = True
    except Exception as e:
        pass

if __name__ == "__main__":
    signal.signal(signal.SIGINT, termination_handler)
    signal.signal(signal.SIGTERM, termination_handler)

    parser = ArgumentParser()
    parser.add_argument("--project-name", action="store", type=str, dest="project_name")
    parser.add_argument("--worker-id", action="store", type=str, dest="worker_id")
    parser.add_argument("--logger-name", action="store", type=str, dest="logger_name")
    parser.add_argument("--kafka-input-args", action="store", type=str, dest="kafka_input_args")
    parser.add_argument("--kafka-output-args", action="store", type=str, dest="kafka_output_args")
    args, _ = parser.parse_known_args()

    logger = logging.getLogger(args.logger_name)
    log_stdout = logging.StreamHandler(sys.stdout)
    logger.addHandler(log_stdout)
    logger.setLevel(logging.INFO)
    g_logger = logger

    with open(os.path.join(config['projects_path'], args.project_name, 'master_config.json')) as f:
        master_config = json.loads(f.read())
    kafka_input_args = json.loads(args.kafka_input_args) if args.kafka_input_args else dict()
    kafka_output_args = json.loads(args.kafka_output_args) if args.kafka_output_args else dict()
    em_paths = [
        os.path.join(config['projects_path'], args.project_name, 'working_dir/additional_ems'),
        os.path.join(config['projects_path'], args.project_name, 'working_dir/generated_em')
    ]

    try:
        etk_worker = ETKWorker(master_config=master_config, em_paths=em_paths, logger=logger,
                           worker_id=int(args.worker_id), project_name=args.project_name,
                           kafka_input_args=kafka_input_args, kafka_output_args=kafka_output_args)
        g_etk_worker = etk_worker
        etk_worker.process()
    except:
        logger.exception('etk_worker main')
