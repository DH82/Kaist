"""Orthographic orbit rotations in the current camera coordinate system."""
import math

def multiply(a,b):
 return [[sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

def rotate_view(matrix,yaw,pitch):
 c,s=math.cos(yaw),math.sin(yaw)
 ry=[[c,0,s],[0,1,0],[-s,0,c]]
 c,s=math.cos(pitch),math.sin(pitch)
 rx=[[1,0,0],[0,c,-s],[0,s,c]]
 # Pre-multiplication rotates about screen axes, not fixed world axes.
 return multiply(multiply(rx,ry),matrix)

def initial_rotation():
 return rotate_view([[1,0,0],[0,1,0],[0,0,1]],.4,.5)

def transform(matrix,point):
 return [sum(row[k]*point[k] for k in range(3)) for row in matrix]


def roll_view(matrix,angle):
 c,s=math.cos(angle),math.sin(angle)
 return multiply([[c,-s,0],[s,c,0],[0,0,1]],matrix)
