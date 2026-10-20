"""Motion snapshot processing only; no recorder or network access."""
import timeit
from backend.cameras.motion import MotionState


def run(count):
    state=MotionState()
    message={'WorkState':{'AlarmState':{'VideoMotion':hex((1<<count)-1)}}}
    return timeit.timeit(lambda:state.reconcile(message),number=10000)/10000


if __name__=='__main__':
    print({f'channels{count}_us':round(run(count)*1e6,2) for count in (16,64,256)})
