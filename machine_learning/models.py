from __future__ import annotations
from typing import Any

def default_params(model_name: str, seed: int, n_jobs: int) -> dict[str, Any]:
    if model_name == 'ridge':
        return {'alpha': 10.0}
    if model_name == 'elasticnet':
        return {'loss': 'huber', 'penalty': 'elasticnet', 'alpha': 0.0001, 'l1_ratio': 0.15, 'max_iter': 3000, 'early_stopping': True, 'random_state': seed}
    if model_name == 'svr':
        return {'C': 10.0, 'epsilon': 0.1, 'gamma': 'scale', 'cache_size': 3000}
    if model_name == 'extra_trees':
        return {'n_estimators': 350, 'max_features': 0.6, 'min_samples_leaf': 1, 'n_jobs': n_jobs, 'random_state': seed}
    if model_name == 'random_forest':
        return {'n_estimators': 350, 'max_features': 0.6, 'min_samples_leaf': 1, 'n_jobs': n_jobs, 'random_state': seed}
    if model_name == 'xgboost':
        return {'n_estimators': 650, 'learning_rate': 0.04, 'max_depth': 7, 'min_child_weight': 3, 'subsample': 0.85, 'colsample_bytree': 0.7, 'reg_lambda': 5.0, 'objective': 'reg:squarederror', 'tree_method': 'hist', 'n_jobs': n_jobs, 'random_state': seed}
    if model_name == 'lightgbm':
        return {'n_estimators': 650, 'learning_rate': 0.04, 'num_leaves': 31, 'max_depth': -1, 'min_child_samples': 20, 'subsample': 0.85, 'colsample_bytree': 0.75, 'reg_lambda': 5.0, 'verbosity': -1, 'n_jobs': n_jobs, 'random_state': seed}
    if model_name == 'catboost':
        return {'iterations': 650, 'learning_rate': 0.04, 'depth': 8, 'l2_leaf_reg': 5.0, 'loss_function': 'RMSE', 'verbose': False, 'allow_writing_files': False, 'thread_count': n_jobs, 'random_seed': seed}
    raise ValueError(model_name)

def estimator_for(model_name: str, params: dict[str, Any]) -> Any:
    if model_name == 'ridge':
        from sklearn.linear_model import Ridge
        return Ridge(**params)
    if model_name == 'elasticnet':
        from sklearn.linear_model import SGDRegressor
        return SGDRegressor(**params)
    if model_name == 'svr':
        from sklearn.svm import SVR
        return SVR(**params)
    if model_name == 'extra_trees':
        from sklearn.ensemble import ExtraTreesRegressor
        return ExtraTreesRegressor(**params)
    if model_name == 'random_forest':
        from sklearn.ensemble import RandomForestRegressor
        return RandomForestRegressor(**params)
    if model_name == 'xgboost':
        from xgboost import XGBRegressor
        return XGBRegressor(**params)
    if model_name == 'lightgbm':
        from lightgbm import LGBMRegressor
        return LGBMRegressor(**params)
    if model_name == 'catboost':
        from catboost import CatBoostRegressor
        return CatBoostRegressor(**params)
    raise ValueError(model_name)
