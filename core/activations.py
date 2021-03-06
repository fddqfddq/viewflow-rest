from django.utils import timezone
from django.utils.timezone import now
from viewflow import signals
from viewflow.activation import Activation, STATUS, all_leading_canceled, ViewActivation
from viewflow.exceptions import FlowRuntimeError
from viewflow.token import Token


class ApprovalActivation(ViewActivation):

    def __init__(self, **kwargs):
        self.next_task = None
        self.tasks = []
        self._owner_list = []
        super(ApprovalActivation, self).__init__(**kwargs)

    def assign_tasks(self):
        with self.exception_guard():
            self._owner_list = self.flow_task.owner_list

            if self._owner_list:
                token_source = Token.split_token_source(self.task.token, self.task.pk)
                for owner in self._owner_list:
                    task = self.flow_class.task_class(
                        process=self.process,
                        flow_task=self.flow_task,
                        token=next(token_source),
                        owner=owner,
                        status=STATUS.ASSIGNED,
                        started=timezone.now()
                    )
                    task.save()
                    task.previous.add(self.task)

            elif self.flow_task._ifnone_next_node is not None:
                self.flow_task._ifnone_next_node.activate(prev_activation=self, token=self.task.token)
            else:
                raise FlowRuntimeError(
                    "{} activated with zero and no IfNone nodes specified".format(self.flow_task.name))

    @classmethod
    def activate(cls, flow_task, prev_activation, token):
        flow_class, flow_task = flow_task.flow_class, flow_task

        activation = cls()
        activation.initialize(flow_task, prev_activation.task)
        activation.assign_tasks()

        return activation

    def is_done(self):
        """Check that process can be continued futher.

        Join check the all task state in db with the common token prefix.

        Join node would continue execution if all incoming tasks are DONE or CANCELED.
        """
        result = False

        token = self.task.token

        if self.task.token.is_split_token():
            token = token.get_base_split_token()

            join_prefixes = set(
                prev.token.get_common_split_prefix(token, prev.pk)
                for prev in self.task.previous.exclude(status=STATUS.CANCELED).all())

            if len(join_prefixes) > 1:
                raise FlowRuntimeError(
                    'Multiple tokens {} came to join {}'.format(join_prefixes, self.flow_task.name))

            join_token_prefix = next(iter(join_prefixes))

            active_tasks = self.flow_class.task_class._default_manager \
                .filter(process=self.process, token__startswith=join_token_prefix) \
                .exclude(status__in=[STATUS.DONE, STATUS.CANCELED])

            if not self.flow_task._wait_all:
                result = True
                # cancel other tasks
                for active_task in active_tasks:
                    active_task.set_status(STATUS.CANCELED)
                    active_task.save()
            else:
                result = not active_tasks.exists()

        return result

    @Activation.status.super()
    def done(self):
        """Complete the join within current exception propagation strategy."""
        with self.exception_guard():
            self.task.finished = now()
            self.set_status(STATUS.DONE)
            self.task.save()

            signals.task_finished.send(sender=self.flow_class, process=self.process, task=self.task)

            if self.is_done():
                self.activate_next()

    @Activation.status.super()
    def prepare(self, data=None, user=None):
        """Prepare activation for execution."""
        super(ApprovalActivation, self).prepare.original()

        if user:
            self.task.owner = user

        # management_form_class = self.get_management_form_class()
        # self.management_form = management_form_class(data=data, instance=self.task)

        # if data:
        #     if not self.management_form.is_valid():
        #         raise FlowRuntimeError('Activation metadata is broken {}'.format(self.management_form.errors))
        #     self.task = self.management_form.save(commit=False)
