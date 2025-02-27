# Copyright (C) 2021 Intel Corporation
#
# SPDX-License-Identifier: MIT

import os.path as osp
import glob
import json
from deepdiff import DeepDiff
from .utils import config
import pytest

@pytest.mark.parametrize('path', glob.glob(osp.join(config.ASSETS_DIR, '*.json')))
def test_check_objects_integrity(path):
    with open(path) as f:
        endpoint = osp.basename(path).rsplit('.')[0]
        response = config.get_method('admin1', endpoint, page_size='all')
        json_objs = json.load(f)
        resp_objs = response.json()

        assert DeepDiff(json_objs, resp_objs, ignore_order=True,
            exclude_regex_paths="root\['results'\]\[\d+\]\['last_login'\]") == {}
