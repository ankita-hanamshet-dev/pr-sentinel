#pragma once
#include <vector>

namespace math {

template <typename T>
class Matrix {
 public:
  std::vector<T> data;
  T* ptr = nullptr;
};

}  // namespace math
