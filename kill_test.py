# kill_test.py - prove WHERE a paused graph actually lives.
#
# A "paused" graph is not a sleeping process — it is only a record in the
# checkpointer. This test makes that visible by using TWO processes:
#
#   python kill_test.py start   -> runs log 56 until the interrupt, then the
#                                  process EXITS (this is our "kill")
#   python kill_test.py resume  -> a brand-new process tries to approve the
#                                  paused run using the same thread_id
#
# With MemorySaver (RAM):    resume FAILS  — checkpoints died with process 1.
# With a durable checkpointer: resume WORKS — checkpoints live outside processes.

import sys
import json
from agent import start_analysis, resume_analysis

THREAD_ID = "kill-test"   # same claim ticket in both processes

if sys.argv[1] == "start":
    result = start_analysis(56, THREAD_ID)
    print("paused at interrupt:", "__interrupt__" in result)
    print("process 1 exiting — if the checkpointer is RAM, the pause dies with me")

elif sys.argv[1] == "resume":
    print("process 2: trying to resume thread", repr(THREAD_ID))
    result = resume_analysis(THREAD_ID, True)
    print("\nRESUME SUCCEEDED — report:")
    print(result["report"])
