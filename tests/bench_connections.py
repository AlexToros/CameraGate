"""Pure pending-ticket reprioritization; no SDK or network calls."""
from collections import deque
import timeit
from backend.cameras.connections import ConnectionGate


def run(count):
    gate=ConnectionGate()
    gate.waiting[1]=deque((i,i) for i in range(count))
    selections=(set(range(count//2)),set(range(count//2,count)))
    def change():
        for selection in selections:gate.prioritize(selection)
    return timeit.timeit(change,number=2000)/4000


if __name__=='__main__':
    print({f'pending{count}_us':round(run(count)*1e6,2) for count in (16,64,256)})
