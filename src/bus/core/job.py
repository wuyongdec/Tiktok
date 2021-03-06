# -*- coding: utf-8 -*-
import json
from copy import deepcopy
from datetime import datetime
import threading
from croniter import croniter
from src.bus.log.config import logger
from dag import DAG
from .components import JobState
from src.bus.exceptions.dagobah import DagobahError
from .task import Task
from src.bus.common.util import StrictJSONEncoder


class Job(DAG):
    """ Controller for a collection and graph of Task objects.

    Emitted events:

    job_complete: On successful completion of the job. Returns
    the current serialization of the job with run logs.
    job_failed: On failed completion of the job. Returns
    the current serialization of the job with run logs.
    """

    def __init__(self, parent, backend, job_id, name):
        logger.debug('Starting Job instance constructor with name {0}'.format(name))
        super(Job, self).__init__()

        self.parent = parent
        self.backend = backend
        self.event_handler = self.parent.event_handler
        self.job_id = job_id
        self.name = name
        self.state = JobState()

        # tasks themselves aren't hashable, so we need a secondary lookup
        self.tasks = {}

        self.next_run = None
        self.cron_schedule = None
        self.cron_iter = None
        self.run_log = None
        self.completion_lock = threading.Lock()
        self.notes = None

        self.snapshot = None

        self._set_status('waiting')

        self.commit()

    def commit(self):
        """ Store metadata on this Job to the backend. """
        logger.debug('Committing job {0}'.format(self.name))
        self.backend.commit_job(self._serialize())
        self.parent.commit()

    def add_task(self, command, name=None, **kwargs):
        """ Adds a new Task to the graph with no edges. """

        logger.debug('Adding task with command {0} to job {1}'.format(command, self.name))
        if not self.state.allow_change_graph:
            raise DagobahError("job's graph is immutable in its current state: %s"
                               % self.state.status)

        if name is None:
            name = command
        new_task = Task(self, command, name, **kwargs)
        self.tasks[name] = new_task
        self.add_node(name)
        self.commit()

    def add_dependency(self, from_task_name, to_task_name):
        """ Add a dependency between two tasks. """

        logger.debug('Adding dependency from {0} to {1}'.format(from_task_name, to_task_name))
        if not self.state.allow_change_graph:
            raise DagobahError("job's graph is immutable in its current state: %s"
                               % self.state.status)

        self.add_edge(from_task_name, to_task_name)
        self.commit()

    def delete_task(self, task_name):
        """ Deletes the named Task in this Job. """

        logger.debug('Deleting task {0}'.format(task_name))
        if not self.state.allow_change_graph:
            raise DagobahError("job's graph is immutable in its current state: %s"
                               % self.state.status)

        if task_name not in self.tasks:
            raise DagobahError('task %s does not exist' % task_name)

        self.tasks.pop(task_name)
        self.delete_node(task_name)
        self.commit()

    def delete_dependency(self, from_task_name, to_task_name):
        """ Delete a dependency between two tasks. """

        logger.debug('Deleting dependency from {0} to {1}'.format(from_task_name, to_task_name))
        if not self.state.allow_change_graph:
            raise DagobahError("job's graph is immutable in its current state: %s"
                               % self.state.status)

        self.delete_edge(from_task_name, to_task_name)
        self.commit()

    def schedule(self, cron_schedule, base_datetime=None):
        """ Schedules the job to run periodically using Cron syntax. """

        logger.debug('Scheduling job {0} with cron schedule {1}'.format(self.name, cron_schedule))
        if not self.state.allow_change_schedule:
            raise DagobahError("job's schedule cannot be changed in state: %s"
                               % self.state.status)

        if cron_schedule is None:
            self.cron_schedule = None
            self.cron_iter = None
            self.next_run = None

        else:
            if base_datetime is None:
                base_datetime = datetime.utcnow()
            self.cron_schedule = cron_schedule
            self.cron_iter = croniter(cron_schedule, base_datetime)
            self.next_run = self.cron_iter.get_next(datetime)

        logger.debug('Determined job {0} next run of {1}'.format(self.name, self.next_run))
        self.commit()

    def start(self):
        """ Begins the job by kicking off all tasks with no dependencies. """

        logger.info('Job {0} starting job run'.format(self.name))
        if not self.state.allow_start:
            raise DagobahError('job cannot be started in its current state; ' +
                               'it is probably already running')

        self.initialize_snapshot()

        # don't increment if the job was run manually
        if self.cron_iter and datetime.utcnow() > self.next_run:
            self.next_run = self.cron_iter.get_next(datetime)

        self.run_log = {'job_id': self.job_id,
                        'name': self.name,
                        'parent_id': self.parent.dagobah_id,
                        'log_id': self.backend.get_new_log_id(),
                        'start_time': datetime.utcnow(),
                        'tasks': {}}
        self._set_status('running')

        logger.debug('Job {0} resetting all tasks prior to start'.format(self.name))
        for task in self.tasks.values():
            task.reset()

        logger.debug('Job {0} seeding run logs'.format(self.name))
        for task_name in self.ind_nodes(self.snapshot):
            self._put_task_in_run_log(task_name)
            self.tasks[task_name].start()

        self._commit_run_log()

    def retry(self):
        """ Restarts failed tasks of a job. """

        logger.info('Job {0} retrying all failed tasks'.format(self.name))
        self.initialize_snapshot()

        failed_task_names = []
        for task_name, log in self.run_log['tasks'].items():
            if log.get('success', True) == False:
                failed_task_names.append(task_name)

        if len(failed_task_names) == 0:
            raise DagobahError('no failed tasks to retry')

        self._set_status('running')
        self.run_log['last_retry_time'] = datetime.utcnow()

        logger.debug('Job {0} seeding run logs'.format(self.name))
        for task_name in failed_task_names:
            self._put_task_in_run_log(task_name)
            self.tasks[task_name].start()

        self._commit_run_log()

    def terminate_all(self):
        """ Terminate all currently running tasks. """
        logger.info('Job {0} terminating all currently running tasks'.format(self.name))
        for task in self.tasks.values():
            if task.started_at and not task.completed_at:
                task.terminate()

    def kill_all(self):
        """ Kill all currently running jobs. """
        logger.info('Job {0} killing all currently running tasks'.format(self.name))
        for task in self.tasks.values():
            if task.started_at and not task.completed_at:
                task.kill()

    def edit(self, **kwargs):
        """ Change this Job's name.

        This will affect the historical data available for this
        Job, e.g. past run logs will no longer be accessible.
        """

        logger.debug('Job {0} changing name to {1}'.format(self.name, kwargs.get('name')))
        if not self.state.allow_edit_job:
            raise DagobahError('job cannot be edited in its current state')

        if 'name' in kwargs and isinstance(kwargs['name'], str):
            if not self.parent._name_is_available(kwargs['name']):
                raise DagobahError('new job name %s is not available' %
                                   kwargs['name'])

        for key in ['name']:
            if key in kwargs and isinstance(kwargs[key], str):
                setattr(self, key, kwargs[key])

        self.parent.commit(cascade=True)

    def update_job_notes(self, notes):
        logger.debug('Job {0} updating notes'.format(self.name))
        if not self.state.allow_edit_job:
            raise DagobahError('job cannot be edited in its current state')

        setattr(self, 'notes', notes)

        self.parent.commit(cascade=True)

    def edit_task(self, task_name, **kwargs):
        """ Change the name of a Task owned by this Job.

        This will affect the historical data available for this
        Task, e.g. past run logs will no longer be accessible.
        """

        logger.debug('Job {0} editing task {1}'.format(self.name, task_name))
        if not self.state.allow_edit_task:
            raise DagobahError("tasks cannot be edited in this job's " +
                               "current state")

        if task_name not in self.tasks:
            raise DagobahError('task %s not found' % task_name)

        if 'name' in kwargs and isinstance(kwargs['name'], str):
            if kwargs['name'] in self.tasks:
                raise DagobahError('task name %s is unavailable' %
                                   kwargs['name'])

        task = self.tasks[task_name]
        for key in ['name', 'command']:
            if key in kwargs and isinstance(kwargs[key], str):
                setattr(task, key, kwargs[key])

        if 'soft_timeout' in kwargs:
            task.set_soft_timeout(kwargs['soft_timeout'])

        if 'hard_timeout' in kwargs:
            task.set_hard_timeout(kwargs['hard_timeout'])

        if 'hostname' in kwargs:
            task.set_hostname(kwargs['hostname'])

        if 'name' in kwargs and isinstance(kwargs['name'], str):
            self.rename_edges(task_name, kwargs['name'])
            self.tasks[kwargs['name']] = task
            del self.tasks[task_name]

        self.parent.commit(cascade=True)

    def _complete_task(self, task_name, **kwargs):
        """ Marks this task as completed. Kwargs are stored in the run log. """

        logger.debug('Job {0} marking task {1} as completed'.format(self.name, task_name))
        self.run_log['tasks'][task_name] = kwargs

        for node in self.downstream(task_name, self.snapshot):
            self._start_if_ready(node)

        try:
            self.backend.acquire_lock()
            self._commit_run_log()
        except:
            logger.exception("Error in handling events.")
        finally:
            self.backend.release_lock()

        if kwargs.get('success', None) == False:
            task = self.tasks[task_name]
            try:
                self.backend.acquire_lock()
                if self.event_handler:
                    self.event_handler.emit('task_failed',
                                            task._serialize(include_run_logs=True))
            except:
                logger.exception("Error in handling events.")
            finally:
                self.backend.release_lock()

        self._on_completion()

    def _put_task_in_run_log(self, task_name):
        """ Initializes the run log task entry for this task. """
        logger.debug('Job {0} initializing run log entry for task {1}'.format(self.name, task_name))
        data = {'start_time': datetime.utcnow(),
                'command': self.tasks[task_name].command}
        self.run_log['tasks'][task_name] = data

    def _is_complete(self):
        """ Returns Boolean of whether the Job has completed. """
        for log in self.run_log['tasks'].values():
            if 'success' not in log:  # job has not returned yet
                return False
        return True

    def _on_completion(self):
        """ Checks to see if the Job has completed, and cleans up if it has. """

        logger.debug('Job {0} running _on_completion check'.format(self.name))
        if self.state.status != 'running' or (not self._is_complete()):
            return

        for job, results in self.run_log['tasks'].items():
            if results.get('success', False) == False:
                self._set_status('failed')
                try:
                    self.backend.acquire_lock()
                    if self.event_handler:
                        self.event_handler.emit('job_failed',
                                                self._serialize(include_run_logs=True))
                except:
                    logger.exception("Error in handling events.")
                finally:
                    self.backend.release_lock()
                break

        if self.state.status != 'failed':
            self._set_status('waiting')
            self.run_log = {}
            try:
                self.backend.acquire_lock()
                if self.event_handler:
                    self.event_handler.emit('job_complete',
                                            self._serialize(include_run_logs=True))
            except:
                logger.exception("Error in handling events.")
            finally:
                self.backend.release_lock()

        self.destroy_snapshot()

    def _start_if_ready(self, task_name):
        """ Start this task if all its dependencies finished successfully. """
        logger.debug('Job {0} running _start_if_ready for task {1}'.format(self.name, task_name))
        task = self.tasks[task_name]
        dependencies = self._dependencies(task_name, self.snapshot)
        for dependency in dependencies:
            if self.run_log['tasks'].get(dependency, {}).get('success', False) == True:
                continue
            return
        self._put_task_in_run_log(task_name)
        task.start()

    def _dependencies(self, task_name, snapshot):
        dependencies = []
        for k, v in snapshot.items():
            if task_name in v:
                dependencies.append(k)

        return dependencies

    def _set_status(self, status):
        """ Enforces enum-like behavior on the status field. """
        try:
            self.state.set_status(status)
        except:
            raise DagobahError('could not set status %s' % status)

    def _commit_run_log(self):
        """" Commit the current run log to the backend. """
        logger.debug('Committing run log for job {0}'.format(self.name))
        self.backend.commit_log(self.run_log)

    def _serialize(self, include_run_logs=False, strict_json=False):
        """ Serialize a representation of this Job to a Python dict object. """

        # return tasks in sorted order if graph is in a valid state
        try:
            topo_sorted = self.topological_sort()
            t = [self.tasks[task]._serialize(include_run_logs=include_run_logs,
                                             strict_json=strict_json)
                 for task in topo_sorted]
        except:
            t = [task._serialize(include_run_logs=include_run_logs,
                                 strict_json=strict_json)
                 for task in self.tasks.values()]

        dependencies = {}
        for k, v in self.graph.items():
            dependencies[k] = list(v)

        result = {'job_id': self.job_id,
                  'name': self.name,
                  'parent_id': self.parent.dagobah_id,
                  'tasks': t,
                  'dependencies': dependencies,
                  'status': self.state.status,
                  'cron_schedule': self.cron_schedule,
                  'next_run': self.next_run,
                  'notes': self.notes}

        if strict_json:
            result = json.loads(json.dumps(result, cls=StrictJSONEncoder))
        return result

    def initialize_snapshot(self):
        """ Copy the DAG and validate """
        logger.debug('Initializing DAG snapshot for job {0}'.format(self.name))
        if self.snapshot is not None:
            logger.warn("Attempting to initialize DAG snapshot without " +
                        "first destroying old snapshot.")

        snapshot_to_validate = deepcopy(self.graph)

        is_valid, reason = self.validate(snapshot_to_validate)
        if not is_valid:
            raise DagobahError(reason)

        self.snapshot = snapshot_to_validate

    def destroy_snapshot(self):
        """ Destroy active copy of the snapshot """
        logger.debug('Destroying DAG snapshot for job {0}'.format(self.name))
        self.snapshot = None
