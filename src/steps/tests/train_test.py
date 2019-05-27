import pytest

from src.steps.method_training import MethodTraining

class TestTrainingMethods:

    @staticmethod
    @pytest.mark.parametrize(
        "method",
        [
            'baseline',
            'baseline++',
            'protonet',
            'matchingnet',
            'relationnet',
            'relationnet_softmax',
            # TODO 'maml', 'maml_approx',
        ])
    def test_step_does_not_return_error(method):
        args_without_method = [
            '--dataset',
            'CUB',
            '--model',
            'Conv4',
            '--num_classes',
            '4412',
            '--stop_epoch',
            '1',
            '--shallow',
            'True',
            '--method',
        ]

        MethodTraining(args_without_method+[method]).apply()
