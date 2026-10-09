import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from models import default_params, estimator_for


def read_features(path):
    frame = pd.read_csv(path)
    if frame.empty:
        raise ValueError('Input must contain at least one row.')
    if not all(pd.api.types.is_numeric_dtype(frame[column]) for column in frame):
        raise ValueError('Provide an already prepared numeric feature table.')
    if not np.isfinite(frame.to_numpy(dtype=float)).all():
        raise ValueError('Feature and target values must be finite.')
    return frame


def main():
    parser = argparse.ArgumentParser(description='Fit and apply environmental kinetics regressors to prepared feature tables.')
    commands = parser.add_subparsers(dest='command', required=True)
    train = commands.add_parser('train')
    train.add_argument('--input', required=True, type=Path)
    train.add_argument('--output', required=True, type=Path)
    train.add_argument('--target', default='target_model')
    train.add_argument('--endpoint', choices=['hl', 'kp', 'qy_new_log10', 'kp_augmented_hl'])
    train.add_argument('--model', choices=['ridge', 'elasticnet', 'svr', 'extra_trees', 'random_forest', 'xgboost', 'lightgbm', 'catboost'])
    train.add_argument('--seed', type=int, default=42)
    train.add_argument('--n-jobs', type=int, default=1)
    predict = commands.add_parser('predict')
    predict.add_argument('--input', required=True, type=Path)
    predict.add_argument('--model-file', required=True, type=Path)
    predict.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    frame = read_features(args.input)
    if args.command == 'train':
        if args.target not in frame:
            raise ValueError(f'Missing target column: {args.target}')
        features = frame.drop(columns=[args.target])
        if not len(features.columns):
            raise ValueError('At least one feature column is required.')
        if args.endpoint:
            configs = json.loads(Path(__file__).with_name('parameters.json').read_text())
            selected = configs[args.endpoint]
            model_name = selected['model']
            if args.model and args.model != model_name:
                raise ValueError('Use either an endpoint preset or a different model, not both.')
            params = default_params(model_name, args.seed, args.n_jobs)
            params.update(selected['params'])
        else:
            model_name = args.model or 'xgboost'
            params = default_params(model_name, args.seed, args.n_jobs)
        estimator = estimator_for(model_name, params)
        estimator.fit(features, frame[args.target].to_numpy(dtype=float))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({'estimator': estimator, 'feature_names': list(features.columns), 'target': args.target,
                     'model': model_name, 'params': params, 'seed': args.seed, 'endpoint': args.endpoint}, args.output)
    else:
        artifact = joblib.load(args.model_file)
        if list(frame.columns) != artifact['feature_names']:
            raise ValueError('Prediction features must have the same names and order as the training features.')
        result = pd.DataFrame({'prediction_model': artifact['estimator'].predict(frame)})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, index=False)
    print(args.output)


if __name__ == '__main__':
    main()
