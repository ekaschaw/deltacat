from __future__ import annotations
from typing import Any, Dict, List, cast, Optional
from deltacat.utils.ray_utils.retry_handler.ray_remote_tasks_batch_scaling_strategy import RayRemoteTasksBatchScalingStrategy
import ray
import time
import logging
from deltacat.logs import configure_logger
from deltacat.utils.ray_utils.retry_handler.task_execution_error import RayRemoteTaskExecutionError
from deltacat.utils.ray_utils.retry_handler.task_info_object import TaskInfoObject
from deltacat.utils.ray_utils.retry_handler.retry_strategy_config import get_retry_strategy_config_for_known_exception

logger = configure_logger(logging.getLogger(__name__))

@ray.remote
def submit_single_task(taskObj: TaskInfoObject, TaskContext: Optional[Interface] = None) -> Any:
    """
     Submits a single task for execution, handles any exceptions that may occur during execution,
     and applies appropriate retry strategies if they are defined.
    """
    try:
        taskObj.attempt_count += 1
        curr_attempt = taskObj.attempt_count
        logger.debug(f"Executing the submitted Ray remote task as part of attempt number: {current_attempt_number}")
        return taskObj.task_callable(taskObj.task_input)
    except (Exception) as exception:
        exception_retry_strategy_config = get_retry_strategy_config_for_known_exception(exception, taskObj.exception_retry_strategy_configs)
        if exception_retry_strategy_config is not None:
            return TaskExecutionError(exception_retry_strategy_config.exception, taskObj)

        logger.error(f"The exception thrown by submitted Ray task during attempt number: {current_attempt_number} is non-retryable or unexpected, hence throwing Non retryable exception: {exception}")
        raise UnexpectedRayTaskError(str(exception))

class RayTaskSubmissionHandler:
    """
    Starts execution of all given a list of Ray tasks with optional arguments: scaling strategy and straggler detection
    """
    def start_tasks_execution(self,
                              ray_remote_task_infos: List[TaskInfoObject],
                              scaling_strategy: Optional[BatchScalingStrategy] = None,
                              straggler_detection: Optional[StragglerDetectionInterface] = None,
                              retry_strategy: Optional[RetryTaskInterface] = None,
                              task_context: Optional[TaskContext] = None) -> None:
    """
    Prepares and initiates the execution of a batch of tasks and can optionally support
    custom client batch scaling, straggler detection, and task context
    """
        if scaling_strategy is None:
            scaling_strategy = AIMDBasedBatchScalingStrategy(ray_remote_task_infos, 50, 100, 10, 2, 0.5)
        if retry_strategy is None:
            retry_strategy = RetryTaskDefault(max_retries = 3)

        active_tasks = []
        attempts = {task.task_id: 0 for task in ray_remote_task_infos}

        while scaling_strategy.has_next_batch():
            current_batch = scaling_strategy.next_batch()
            for task in current_batch:
                self._submit_task(task)
                active_tasks.append(task) #maybe should be task_id

            while active_tasks:
                completed_task = self._get_task_results(1)
                if isInstance(completed_task, TaskExecutionError):
                    scaling_strategy.mark_task_failed(completed_task)
                    if retry_strategy.should_retry(task, completed_task): #add max retry here
                        attempts[task.task_id] += 1
                        self.ray_remote_task_infos.append(completed_task)
                        active_tasks.remove(completed_task)
                else:
                    scaling_strategy.mark_task_complete(completed_task)
                    active_tasks.remove(task)
                        # If straggler detection is enabled, iterate over the active tasks again
            if straggler_detection is not None:
                for task in active_tasks[:]:
                    if straggler_detection.is_straggler(task, task_context):
                        ray.cancel(task)
                        active_tasks.remove(task)
                        # If you want to re-add the cancelled stragglers to the task queue
                        self.ray_remote_task_infos.append(task)

    def _wait_and_get_all_task_results(self) -> List[Any]:
        return self._get_task_results(self.num_of_submitted_tasks)

    def _get_task_results(self, num_of_results: int) -> List[Any]:
        """
        Gets results from a list of tasks to be executed, and catches exceptions to manage the retry strategy.
        Optional: Given a StragglerDetectionInterface, can detect and handle straggler tasks according to the client logic
        """
        if not self.unfinished_promises or num_of_results == 0:
            return []
        elif num_of_results > len(self.unfinished_promises):
            num_of_results = len(self.unfinished_promises)

        finished, self.unfinished_promises = ray.wait(self.unfinished_promises, num_of_results)
        successful_results = []

        for finished in finished:
            finished_result = None
            try:
                finished_result = ray.get(finished)
            except (Exception) as exception:
                #if exception send to method handle_ray_exception to determine what to do and assign the corresp error
                finished_result = self._handle_ray_exception(exception=exception, ray_remote_task_info=self.task_promise_obj_ref_to_task_info_map[str(finished_promise)] )#evaluate the exception and return the error

                exception_retry_strategy_config = get_retry_strategy_config_for_known_exception(finished_result.exception,
                     finished_result.ray_remote_task_info.exception_retry_strategy_configs)
                if (exception_retry_strategy_config is None or finished_result.ray_remote_task_info.num_of_attempts > exception_retry_strategy_config.max_retry_attempts):
                    logger.error(f"The submitted task has exhausted all the maximum retries configured and finally throws exception - {finished_result.exception}")
                    raise finished_result.exception
                self._update_ray_remote_task_options_on_exception(finished_result.exception, finished_result.ray_remote_task_info)
                self.unfinished_promises.append(self._invoke_ray_remote_task(ray_remote_task_info=finished_result.ray_remote_task_info))
            else:
                successful_results.append(finished_result)
                del self.task_promise_obj_ref_to_task_info_map[str(finished_promise)]

        num_of_successful_results = len(successful_results)
        self.num_of_submitted_tasks_completed += num_of_successful_results
        self.current_batch_size -= num_of_successful_results

        self._enqueue_new_tasks(num_of_successful_results)

        if num_of_successful_results < num_of_results:
            successful_results.extend(self._get_task_results(num_of_results - num_of_successful_results))
            return successful_results
        else:
            return successful_results


    def _enqueue_new_tasks(self, num_of_tasks: int) -> None:
        """
        Helper method to submit a specified number of tasks
        """
        new_tasks_submitted = self.remaining_ray_remote_task_infos[:num_of_tasks]
        num_of_new_tasks_submitted = len(new_tasks_submitted)
        self._submit_tasks(new_tasks_submitted)
        self.remaining_ray_remote_task_infos = self.remaining_ray_remote_task_infos[num_of_tasks:]
        self.current_batch_size += num_of_new_tasks_submitted
        logger.info(f"Enqueued {num_of_new_tasks_submitted} new tasks. Current concurrency of tasks execution: {self.current_batch_size}, Current Task progress: {self.num_of_submitted_tasks_completed}/{self.num_of_submitted_tasks}")

    def _submit_tasks(self, info_objs: List[TaskInfoObject]) -> None:
        for info_obj in info_objs:
            time.sleep(0.005)
            self.unfinished_promises.append(self._invoke_ray_remote_task(info_obj))

    #replace with ray.options
    def _invoke_ray_remote_task(self, ray_remote_task_info: RayRemoteTaskInfo) -> Any:
        #change to using ray.options
        ray_remote_task_options_arguments = dict()

        if ray_remote_task_info.ray_remote_task_options.memory:
            ray_remote_task_options_arguments['memory'] = ray_remote_task_info.ray_remote_task_options.memory

        if ray_remote_task_info.ray_remote_task_options.num_cpus:
            ray_remote_task_options_arguments['num_cpus'] = ray_remote_task_info.ray_remote_task_options.num_cpus

        if ray_remote_task_info.ray_remote_task_options.placement_group:
            ray_remote_task_options_arguments['placement_group'] = ray_remote_task_info.ray_remote_task_options.placement_group

        ray_remote_task_promise_obj_ref = submit_single_task.options(**ray_remote_task_options_arguments).remote(ray_remote_task_info=ray_remote_task_info)
        self.task_promise_obj_ref_to_task_info_map[str(ray_remote_task_promise_obj_ref)] = ray_remote_task_info

        return ray_remote_task_promise_obj_ref

    #replace with ray.options
    def _update_ray_remote_task_options_on_exception(self, exception: Exception, ray_remote_task_info: RayRemoteTaskInfo):
        exception_retry_strategy_config = get_retry_strategy_config_for_known_exception(exception, ray_remote_task_info.exception_retry_strategy_configs)
        if exception_retry_strategy_config and ray_remote_task_info.ray_remote_task_options.memory:
            logger.info(f"Updating the Ray remote task options after encountering exception: {exception}")
            ray_remote_task_memory_multiply_factor = exception_retry_strategy_config.ray_remote_task_memory_multiply_factor
            ray_remote_task_info.ray_remote_task_options.memory *= ray_remote_task_memory_multiply_factor
            logger.info(f"Updated ray remote task options Memory: {ray_remote_task_info.ray_remote_task_options.memory}")
    #replace with own exceptions
    def _handle_ray_exception(self, exception: Exception, ray_remote_task_info: RayRemoteTaskInfo) -> RayRemoteTaskExecutionError:
        logger.error(f"Ray remote task failed with {type(exception)} Ray exception: {exception}")
        if type(exception).__name__ == "AWSSecurityTokenRateExceededException(RetryableError)"