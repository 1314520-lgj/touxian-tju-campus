"""A reservation-of-rights clause is not a cancellation.

The recruitment notice at job.tju.edu.cn carries the standard disclaimer:

    集团有权根据岗位需求变化及报名情况等因素，调整、取消或终止个别岗位的招聘工作

The extractor read 取消 as a cancellation, so the notice became a ``cancel``
candidate with no linkable original — and a cancellation without a target can
never be published. A live notice with a 2026-11-13 deadline was therefore
dropped silently, which is worse than a noisy list: the student never learns it
existed.

Two signals separate a disclaimer from an act:

* the sentence reserves a right (有权 / 保留…权利 / 最终解释权);
* the verb appears in a coordinated list of the organiser's options
  (调整、取消或终止), which describes what they *may* do, not what happened.

The genuine cancellation the product already detects must keep working.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.campus.content import cancellation_value  # noqa: E402

DISCLAIMER = (
    "（二）集团有权根据岗位需求变化及报名情况等因素，调整、取消或终止个别岗位的招聘工作"
)
GENUINE = "根据天津市和天津大学的防疫要求，天津大学研究生院取消2022年12月份针对社会面的雅思考试"


def test_a_reservation_of_rights_is_not_a_cancellation():
    assert cancellation_value(DISCLAIMER) is False


def test_a_coordinated_list_of_options_is_not_a_cancellation():
    """Without 有权, the enumerated alternatives still describe a capability."""
    assert cancellation_value("招聘单位可调整、取消或终止个别岗位的招聘工作") is False
    assert cancellation_value("主办方有权变更、取消或终止本次活动") is False


def test_a_genuine_cancellation_is_still_detected():
    assert cancellation_value(GENUINE) is True


def test_a_plain_cancellation_sentence_is_still_detected():
    assert cancellation_value("经研究决定，取消原定于2026年11月5日举办的宣讲会") is True


def test_earlier_exclusions_still_hold():
    # conditional
    assert cancellation_value("如遇疫情变化，考点可能取消当次考试") is False
    # the student cancels their own submission
    assert cancellation_value("可取消已提交的申请") is False
    # form controls
    assert cancellation_value("点击删除按钮取消") is False
    assert cancellation_value("取消勾选“接收通知”选项") is False
    # negation
    assert cancellation_value("本次活动不会取消") is False