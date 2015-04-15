"""
    sqmpy.job.saga_helper
    ~~~~~

    Provides ways to interact with saga classes
"""
import os
import time
import datetime
import hashlib

import saga
from flask import current_app, copy_current_request_context

import helpers
from ..models import db
from .constants import FileRelation, ScriptType, Adaptor
from .exceptions import JobManagerException
from .models import Resource, StagingFile, JobStateHistory, Job
from .callback import JobStateChangeCallback

__author__ = 'Mehdi Sadeghi'


class SagaJobWrapper(object):
    """
    To wrap, initialize and run a saga job.
    """
    def __init__(self, job):
        """
        Init
        :param job: an instance of job model class
        :return:
        """
        self._job_id = job.id
        self._job = job

        # If job has resource_job_id, connect to it
        if job.remote_job_id:
            self._job_service = self.make_job_service(job.resource_endpoint)
            self._saga_job = self._job_service.get_job(job.remote_job_id)
        else:
            # Creating the job service object i.e. the connection
            job.resource_endpoint = get_resource_endpoint(job.resource.url, job.script_type)
            self._job_service = self.make_job_service(job.resource_endpoint)

    def make_job_service(self, endpoint):
        # Create ssh security context
        ctx = saga.Context('ssh')
        session = saga.Session()
        session.add_context(ctx)

        return saga.job.Service(endpoint,
                                session=session)

    def _register_callbacks(self):
        """
        Register callback functions for the saga job
        :return:
        """
        # This callback will locally store output files and new states in db
        self._saga_job.add_callback(saga.STATE, JobStateChangeCallback(self._job, self))

    def get_job(self):
        """
        Returns the inner job
        :return:
        """
        return self._job

    def run(self):
        """
        Run the job on remote resource
        :return:
        """
        # Get resource address
        resource = None
        if self._job.resource_id and self._job.resource_id > 0:
            resource = Resource.query.get(self._job.resource_id)

        # Set remote job working directory
        remote_job_dir = get_job_endpoint(self._job.id, self._job_service.session)
        if remote_job_dir.list():
            raise JobManagerException('Remote directory is not empty')

        # transfer job files to remote directory
        transfer_job_files(self._job.id, remote_job_dir.get_url())

        # Create saga job description
        jd = make_job_description(self._job, remote_job_dir)

        # Create saga job
        self._saga_job = self._job_service.create_job(jd)

        # Register call backs
        #self._register_callbacks()

        # TODO: My monitoring approach is wrong and should be changed.
        # Prepare our gevent greenlet
        import gevent
        @copy_current_request_context
        def monitor_state():
            while True:
                gevent.sleep(3)
                try:
                    val = self._saga_job.state
                    if val != self._job.last_status:
                        # Shout out load
                        try:
                            helpers.send_state_change_email(self._job.id,
                                                            self._job.owner_id,
                                                            self._job.last_status,
                                                            val)
                        except Exception, ex:
                            current_app.logger.debug("Callback: Failed to send mail: %s" % ex)
                        # Insert history record
                        history_record = JobStateHistory()
                        history_record.change_time = datetime.datetime.now()
                        history_record.old_state = self._job.last_status
                        history_record.new_state = val
                        history_record.job_id = self._job.id
                        db.session.add(history_record)
                        db.session.flush()
                        # If there are new files, transfer them back, along with output and error files
                        download_job_files(self._job.id,
                                           self._saga_job.description,
                                           self._job_service.session)
                        # Update last status
                        self._job.last_status = val
                        if self._job not in db.session:
                            db.session.merge(self._job)
                        current_app.logger.debug('Before commit the new value is %s ' % val)
                        db.session.commit()
                    if val in (saga.FAILED,
                               saga.DONE,
                               saga.CANCELED,
                               saga.FINAL,
                               saga.EXCEPTION):
                        print 'Breaking ...', val
                        return
                except saga.IncorrectState:
                    pass
        gevent.spawn(monitor_state)

        # Run the job eventually
        current_app.logger.debug("...starting job...")
        self._saga_job.run()

        # Store remote pid
        self._job.remote_job_id = self._saga_job.get_id()
        db.session.commit()

        current_app.logger.debug("Job ID    : %s" % self._saga_job.id)
        current_app.logger.debug("Job State : %s" % self._saga_job.state)

    def cancel(self):
        """
        Cancel the job
        """
        self._saga_job.cancel()


def get_resource_endpoint(host, adaptor):
    """
    Get ssh URI of remote host
    :param host: host to make url for it
    :param adaptor: adaptor integer value according to Adaptor enum
    :return:
    """
    backend = 'ssh'
    if helpers.is_localhost(host):
        backend = 'fork'
    elif adaptor == Adaptor.sge.value:
        backend = 'sge+ssh'
    return '{backend}://{remote_host}'.format(backend=backend,
                                              remote_host=host)


def get_job_endpoint(job_id, session):
    """
    Returns the remote job working directory. Creates the parent
    folders if they don't exist.
    :param job_id: job id
    :param session: saga session to be used
    :return:
    """
    job = Job.query.get(job_id)
    # Remote working directory will be inside temp folder
    if not job.remote_dir:
        job.remote_dir = '/tmp/sqmpy/{job_id}'.format(job_id=job.id)
    adapter = 'sftp'
    if helpers.is_localhost(job.resource.url):
        adapter = 'file'
    remote_address = \
        '{adapter}://{remote_host}/{working_directory}'.format(adapter=adapter,
                                                               remote_host=job.resource.url,
                                                               working_directory=job.remote_dir)
    # Appropriate folders will be created
    return \
        saga.filesystem.Directory(remote_address,
                                  saga.filesystem.CREATE_PARENTS,
                                  session=session)


def make_job_description(job, remote_job_dir):
    """
    Creates saga job description
    :param job: job instance
    :param remote_job_dir: saga remote job directory instance
    :return:
    """
    script_file = \
        StagingFile.query.filter(StagingFile.parent_id == job.id,
                                 StagingFile.relation == FileRelation.script.value).first()

    jd = saga.job.Description()
    # TODO: Add queue name, project and other params
    jd.working_directory = remote_job_dir.get_url().path
    jd.total_cpu_count = job.total_cpu_count
    jd.wall_time_limit = job.walltime_limit
    jd.spmd_variation = job.spmd_variation
    jd.queue = job.queue or None
    jd.total_physical_memory = job.total_physical_memory or None
    jd.project = job.project or None

    # TODO: Add proper arguments for each input file
    jd.arguments = [script_file.name]

    # TODO: Use script handler instead, see issue #13 on github
    if job.script_type == ScriptType.python.value:
        jd.executable = '/usr/bin/python'
    if job.script_type == ScriptType.shell.value:
        jd.executable = '/bin/sh'
    script_abs_path = '{dir}/{file}'.format(dir=remote_job_dir.get_url().path,
                                            file=script_file.name)
    jd.arguments = [script_abs_path]

    jd.output = '{script_name}.out.txt'.format(script_name=script_file.name)
    jd.error = '{script_name}.err.txt'.format(script_name=script_file.name)
    return jd


def download_job_files(job_id, job_description, session, config=None, wipe=True):
    """
    Copies output and error files along with any other output files back to the current machine.
    :param job_id: job id
    :param job_description:
    :param session: saga session to remote resource
    :param wipe: if set to True will wipe files from remote machine.
    :return:
    """
    # Get a new object in this session
    job = Job.query.get(job_id)

    # Get or create job directory
    local_job_dir = helpers.get_job_staging_folder(job_id, config)
    if helpers.is_localhost(job.resource.url):
        local_job_dir_url = local_job_dir
    else:
        local_job_dir_url = helpers.get_job_staging_folder(job_id, config, make_sftp_url=True)

    # Get staging file names for this job which are already uploaded
    # we don't need to download them since we have them already
    uploaded_files = \
        StagingFile.query.with_entities(StagingFile.name).filter(StagingFile.parent_id == Job.id,
                                                                 Job.id == job_id).all()
    # Convert tuple result to list
    uploaded_files = [file_name for file_name, in uploaded_files]
    remote_dir = get_job_endpoint(job_id, session)
    files = remote_dir.list()
    staging_files = []
    for file_url in files:
        if file_url.path == job_description.output:
            staging_files.append((file_url, FileRelation.output.value))
        elif file_url.path == job_description.error:
            staging_files.append((file_url, FileRelation.error.value))
        elif file_url.path not in uploaded_files:
            staging_files.append((file_url, FileRelation.output.value))

    for file_url, relation in staging_files:
        # Copy physical file to local directory
        if wipe:
            remote_dir.move(file_url, local_job_dir_url)
        else:
            remote_dir.copy(file_url, local_job_dir_url)
        time.sleep(.5)
        # Insert appropriate record into db
        absolute_name = os.path.join(local_job_dir, file_url.path)
        sf = StagingFile()
        sf.name = file_url.path
        sf.relation = relation
        sf.original_name = file_url.path
        sf.checksum = hashlib.md5(open(absolute_name).read()).hexdigest()
        sf.location = local_job_dir
        sf.parent_id = job_id
        db.session.add(sf)
    db.session.commit()


def transfer_job_files(job_id, remote_job_dir_url):
    """
    Upload job files to remote resource
    :param job_id: job id
    :param remote_job_dir_url: saga.url.Url of remote job directory
    :return
    """
    # Copy script and input files to remote host
    uploading_files = \
        StagingFile.query.filter(StagingFile.parent_id == job_id,
                                 StagingFile.relation.in_([FileRelation.input.value,
                                                           FileRelation.script.value])).all()
    for file_to_upload in uploading_files:
        file_wrapper = \
            saga.filesystem.File('file://localhost/{file_path}'
                                 .format(file_path=file_to_upload.get_path()))
        file_wrapper.copy(remote_job_dir_url)
